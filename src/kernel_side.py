import argparse
import collections
import math
import os
import re
import resource
import shutil
import sys
import textwrap
import time
import numpy as np


with open('/sys/kernel/mm/transparent_hugepage/hpage_pmd_size') as f:
    PAGE_SIZE = resource.getpagesize()
    PAGE_SHIFT = int(math.log2(PAGE_SIZE))
    PMD_SIZE = int(f.read())
    PMD_ORDER = int(math.log2(PMD_SIZE / PAGE_SIZE))


def align_forward(v, a):
    return (v + (a - 1)) & ~(a - 1)


def align_offset(v, a):
    return v & (a - 1)


def kbnr(kb):
    # Convert KB to number of pages.
    return (kb << 10) >> PAGE_SHIFT


def nrkb(nr):
    # Convert number of pages to KB.
    return (nr << PAGE_SHIFT) >> 10


def odkb(order):
    # Convert page order to KB.
    return (PAGE_SIZE << order) >> 10


def cont_ranges_all(search, index):
    # Given a list of arrays, find the ranges for which values are monotonically
    # incrementing in all arrays. all arrays in search and index must be the
    # same size.
    sz = len(search[0])
    r = np.full(sz, 2)
    d = np.diff(search[0]) == 1
    for dd in [np.diff(arr) == 1 for arr in search[1:]]:
        d &= dd
    r[1:] -= d
    r[:-1] -= d
    return [np.repeat(arr, r).reshape(-1, 2) for arr in index]


class ArgException(Exception):
    pass


class FileIOException(Exception):
    pass


class BinArrayFile:
    # Base class used to read /proc/<pid>/pagemap and /proc/kpageflags into a
    # numpy array. Use inherrited class in a with clause to ensure file is
    # closed when it goes out of scope.
    def __init__(self, filename, element_size):
        self.element_size = element_size
        self.filename = filename
        self.fd = os.open(self.filename, os.O_RDONLY)

    def cleanup(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()

    def _readin(self, offset, buffer):
        length = os.preadv(self.fd, (buffer,), offset)
        if len(buffer) != length:
            raise FileIOException('error: {} failed to read {} bytes at {:x}'
                            .format(self.filename, len(buffer), offset))

    def _toarray(self, buf):
        assert(self.element_size == 8)
        return np.frombuffer(buf, dtype=np.uint64)

    def getv(self, vec):
        vec *= self.element_size
        offsets = vec[:, 4]
        lengths = (np.diff(vec) + self.element_size).reshape(len(vec))
        buf = bytearray(int(np.sum(lengths)))
        view = memoryview(buf)
        pos = 0
        for offset, length in zip(offsets, lengths):
            offset = int(offset)
            length = int(length)
            self._readin(offset, view[pos:pos+length])
            pos += length
        return self._toarray(buf)

    def get(self, index, nr=1):
        offset = index * self.element_size
        length = nr * self.element_size
        buf = bytearray(length)
        self._readin(offset, buf)
        return self._toarray(buf)


PM_PAGE_PRESENT = 1 << 63
PM_PFN_MASK = (1 << 55) - 1

class PageMap(BinArrayFile):
    # Read ranges of a given pid's pagemap into a numpy array.
    def __init__(self, pid='self'):
        super().__init__(f'/proc/{pid}/pagemap', 8)


KPF_ANON = 1 << 12
KPF_COMPOUND_HEAD = 1 << 15
KPF_COMPOUND_TAIL = 1 << 16
KPF_THP = 1 << 22

class KPageFlags(BinArrayFile):
    # Read ranges of /proc/kpageflags into a numpy array.
    def __init__(self):
         super().__init__(f'/proc/kpageflags', 8)


vma_all_stats = set([
    "Size",
    "Rss",
    "Pss",
    "Pss_Dirty",
    "Shared_Clean",
    "Shared_Dirty",
    "Private_Clean",
    "Private_Dirty",
    "Referenced",
    "Anonymous",
    "KSM",
    "LazyFree",
    "AnonHugePages",
    "ShmemPmdMapped",
    "FilePmdMapped",
    "Shared_Hugetlb",
    "Private_Hugetlb",
    "Swap",
    "SwapPss",
    "Locked",
])

vma_min_stats = set([
    "Rss",
    "Anonymous",
    "AnonHugePages",
    "ShmemPmdMapped",
    "FilePmdMapped",
])

VMA = collections.namedtuple('VMA', [
    'name',
    'start',
    'end',
    'read',
    'write',
    'execute',
    'private',
    'pgoff',
    'major',
    'minor',
    'inode',
    'stats',
])

class VMAList:
    # A container for VMAs, parsed from /proc/<pid>/smaps. Iterate over the
    # instance to receive VMAs.
    def __init__(self, pid='self', stats=[]):
        self.vmas = []
        with open(f'/proc/{pid}/smaps', 'r') as file:
            for line in file:
                elements = line.split()
                if '-' in elements[0]:
                    start, end = map(lambda x: int(x, 16), elements[0].split('-'))
                    major, minor = map(lambda x: int(x, 16), elements[3].split(':'))
                    self.vmas.append(VMA(
                        name=elements[5] if len(elements) == 6 else '',
                        start=start,
                        end=end,
                        read=elements[1][0] == 'r',
                        write=elements[1][1] == 'w',
                        execute=elements[1][2] == 'x',
                        private=elements[1][3] == 'p',
                        pgoff=int(elements[2], 16),
                        major=major,
                        minor=minor,
                        inode=int(elements[4], 16),
                        stats={},
                    ))
                else:
                    param = elements[0][:-1]
                    if param in stats:
                        value = int(elements[1])
                        self.vmas[-1].stats[param] = {'type': None, 'value': value}

    def __iter__(self):
        yield from self.vmas


def thp_parse(vma, kpageflags, ranges, indexes, vfns, pfns, anons, heads):
    # Given 4 same-sized arrays representing a range within a page table backed
    # by THPs (vfns: virtual frame numbers, pfns: physical frame numbers, anons:
    # True if page is anonymous, heads: True if page is head of a THP), return a
    # dictionary of statistics describing the mapped THPs.
    stats = {
        'file': {
            'partial': 0,
            'aligned': [0] * (PMD_ORDER + 1),
            'unaligned': [0] * (PMD_ORDER + 1),
        },
        'anon': {
            'partial': 0,
            'aligned': [0] * (PMD_ORDER + 1),
            'unaligned': [0] * (PMD_ORDER + 1),
        },
    }

    for rindex, rpfn in zip(ranges[0], ranges[2]):
        index_next = int(rindex[0])
        index_end = int(rindex[1]) + 1
        pfn_end = int(rpfn[1]) + 1

        folios = indexes[index_next:index_end][heads[index_next:index_end]]

        # Account pages for any partially mapped THP at the front. In that case,
        # the first page of the range is a tail.
        nr = (int(folios[0]) if len(folios) else index_end) - index_next
        stats['anon' if anons[index_next] else 'file']['partial'] += nr

        # Account pages for any partially mapped THP at the back. In that case,
        # the next page after the range is a tail.
        if len(folios):
            flags = int(kpageflags.get(pfn_end)[0])
            if flags & KPF_COMPOUND_TAIL:
                nr = index_end - int(folios[-1])
                folios = folios[:-1]
                index_end -= nr
                stats['anon' if anons[index_end - 1] else 'file']['partial'] += nr

        # Account fully mapped THPs in the middle of the range.
        if len(folios):
            folio_nrs = np.append(np.diff(folios), np.uint64(index_end - folios[-1]))
            folio_orders = np.log2(folio_nrs).astype(np.uint64)
            for index, order in zip(folios, folio_orders):
                index = int(index)
                order = int(order)
                nr = 1 << order
                vfn = int(vfns[index])
                align = 'aligned' if align_forward(vfn, nr) == vfn else 'unaligned'
                anon = 'anon' if anons[index] else 'file'
                stats[anon][align][order] += nr

    # Account PMD-mapped THPs spearately, so filter out of the stats. There is a
    # race between acquiring the smaps stats and reading pagemap, where memory
    # could be deallocated. So clamp to zero incase it would have gone negative.
    anon_pmd_mapped = vma.stats['AnonHugePages']['value']
    file_pmd_mapped = vma.stats['ShmemPmdMapped']['value'] + \
                      vma.stats['FilePmdMapped']['value']
    stats['anon']['aligned'][PMD_ORDER] = max(0, stats['anon']['aligned'][PMD_ORDER] - kbnr(anon_pmd_mapped))
    stats['file']['aligned'][PMD_ORDER] = max(0, stats['file']['aligned'][PMD_ORDER] - kbnr(file_pmd_mapped))

    rstats = {
        f"anon-thp-pmd-aligned-{odkb(PMD_ORDER)}kB": {'type': 'anon', 'value': anon_pmd_mapped},
        f"file-thp-pmd-aligned-{odkb(PMD_ORDER)}kB": {'type': 'file', 'value': file_pmd_mapped},
    }

    def flatten_sub(type, subtype, stats):
        param = f"{type}-thp-pte-{subtype}-{{}}kB"
        for od, nr in enumerate(stats[2:], 2):
            rstats[param.format(odkb(od))] = {'type': type, 'value': nrkb(nr)}

    def flatten_type(type, stats):
        flatten_sub(type, 'aligned', stats['aligned'])
        flatten_sub(type, 'unaligned', stats['unaligned'])
        rstats[f"{type}-thp-pte-partial"] = {'type': type, 'value': nrkb(stats['partial'])}

    flatten_type('anon', stats['anon'])
    flatten_type('file', stats['file'])

    return rstats


def cont_parse(vma, order, ranges, anons, heads):
    # Given 4 same-sized arrays representing a range within a page table backed
    # by THPs (vfns: virtual frame numbers, pfns: physical frame numbers, anons:
    # True if page is anonymous, heads: True if page is head of a THP), return a
    # dictionary of statistics describing the contiguous blocks.
    nr_cont = 1 << order
    nr_anon = 0
    nr_file = 0

    for rindex, rvfn, rpfn in zip(*ranges):
        index_next = int(rindex[0])
        index_end = int(rindex[1]) + 1
        vfn_start = int(rvfn[0])
        pfn_start = int(rpfn[0])

        if align_offset(pfn_start, nr_cont) != align_offset(vfn_start, nr_cont):
            continue

        off = align_forward(vfn_start, nr_cont) - vfn_start
        index_next += off

        while index_next + nr_cont <= index_end:
            folio_boundary = heads[index_next+1:index_next+nr_cont].any()
            if not folio_boundary:
                if anons[index_next]:
                    nr_anon += nr_cont
                else:
                    nr_file += nr_cont
            index_next += nr_cont

    # Account blocks that are PMD-mapped spearately, so filter out of the stats.
    # There is a race between acquiring the smaps stats and reading pagemap,
    # where memory could be deallocated. So clamp to zero incase it would have
    # gone negative.
    anon_pmd_mapped = vma.stats['AnonHugePages']['value']
    file_pmd_mapped = vma.stats['ShmemPmdMapped']['value'] + \
                    vma.stats['FilePmdMapped']['value']
    nr_anon = max(0, nr_anon - kbnr(anon_pmd_mapped))
    nr_file = max(0, nr_file - kbnr(file_pmd_mapped))

    rstats = {
        f"anon-cont-pmd-aligned-{nrkb(nr_cont)}kB": {'type': 'anon', 'value': anon_pmd_mapped},
        f"file-cont-pmd-aligned-{nrkb(nr_cont)}kB": {'type': 'file', 'value': file_pmd_mapped},
    }

    rstats[f"anon-cont-pte-aligned-{nrkb(nr_cont)}kB"] = {'type': 'anon', 'value': nrkb(nr_anon)}
    rstats[f"file-cont-pte-aligned-{nrkb(nr_cont)}kB"] = {'type': 'file', 'value': nrkb(nr_file)}

    return rstats


def vma_print(vma, pid):
    # Prints a VMA instance in a format similar to smaps. The main difference is
    # that the pid is included as the first value.
    print("{:010d}: {:016x}-{:016x} {}{}{}{} {:08x} {:02x}:{:02x} {:08x} {}"
        .format(
            pid, vma.start, vma.end,
            'r' if vma.read else '-', 'w' if vma.write else '-',
            'x' if vma.execute else '-', 'p' if vma.private else 's',
            vma.pgoff, vma.major, vma.minor, vma.inode, vma.name
        ))


def stats_print(stats, tot_anon, tot_file, inc_empty):
    # Print a statistics dictionary.
    label_field = 32
    for label, stat in stats.items():
        type = stat['type']
        value = stat['value']
        if value or inc_empty:
            pad = max(0, label_field - len(label) - 1)
            if type == 'anon' and tot_anon > 0:
                percent = f' ({value / tot_anon:3.0%})'
            elif type == 'file' and tot_file > 0:
                percent = f' ({value / tot_file:3.0%})'
            else:
                percent = ''
            print(f"{label}:{' ' * pad}{value:8} kB{percent}")


def vma_parse(vma, pagemap, kpageflags, contorders):
    # Generate thp and cont statistics for a single VMA.
    start = vma.start >> PAGE_SHIFT
    end = vma.end >> PAGE_SHIFT

    pmes = pagemap.get(start, end - start)
    present = pmes & PM_PAGE_PRESENT != 0
    pfns = pmes & PM_PFN_MASK
    pfns = pfns[present]
    vfns = np.arange(start, end, dtype=np.uint64)
    vfns = vfns[present]

    pfn_vec = cont_ranges_all([pfns], [pfns])[0]
    flags = kpageflags.getv(pfn_vec)
    anons = flags & KPF_ANON != 0
    heads = flags & KPF_COMPOUND_HEAD != 0
    thps = flags & KPF_THP != 0

    vfns = vfns[thps]
    pfns = pfns[thps]
    anons = anons[thps]
    heads = heads[thps]

    indexes = np.arange(len(vfns), dtype=np.uint64)
    ranges = cont_ranges_all([vfns, pfns], [indexes, vfns, pfns])

    thpstats = thp_parse(vma, kpageflags, ranges, indexes, vfns, pfns, anons, heads)
    contstats = [cont_parse(vma, order, ranges, anons, heads) for order in contorders]

    tot_anon = vma.stats['Anonymous']['value']
    tot_file = vma.stats['Rss']['value'] - tot_anon

    return {
        **thpstats,
        **{k: v for s in contstats for k, v in s.items()}
    }, tot_anon, tot_file


def do_main(args):
    pids = set()
    rollup = {}
    rollup_anon = 0
    rollup_file = 0

    if args.cgroup:
        strict = False
        for walk_info in os.walk(args.cgroup):
            cgroup = walk_info[0]
            with open(f'{cgroup}/cgroup.procs') as pidfile:
                for line in pidfile.readlines():
                    pids.add(int(line.strip()))
    elif args.pid:
        strict = True
        pids = pids.union(args.pid)
    else:
        strict = False
        for pid in os.listdir('/proc'):
            if pid.isdigit():
                pids.add(int(pid))

    if not args.rollup:
        print("       PID             START              END PROT   OFFSET   DEV    INODE OBJECT")

    for pid in pids:
        try:
            with PageMap(pid) as pagemap:
                with KPageFlags() as kpageflags:
                    for vma in VMAList(pid, vma_all_stats if args.inc_smaps else vma_min_stats):
                        if (vma.read or vma.write or vma.execute) and vma.stats['Rss']['value'] > 0:
                            stats, vma_anon, vma_file = vma_parse(vma, pagemap, kpageflags, args.cont)
                        else:
                            stats = {}
                            vma_anon = 0
                            vma_file = 0
                        if args.inc_smaps:
                            stats = {**vma.stats, **stats}
                        if args.rollup:
                            for k, v in stats.items():
                                if k in rollup:
                                    assert(rollup[k]['type'] == v['type'])
                                    rollup[k]['value'] += v['value']
                                else:
                                    rollup[k] = v
                            rollup_anon += vma_anon
                            rollup_file += vma_file
                        else:
                            vma_print(vma, pid)
                            stats_print(stats, vma_anon, vma_file, args.inc_empty)
        except (FileNotFoundError, ProcessLookupError, FileIOException):
            if strict:
                raise

    if args.rollup:
        stats_print(rollup, rollup_anon, rollup_file, args.inc_empty)


def main():
    docs_width = shutil.get_terminal_size().columns
    docs_width -= 2
    docs_width = min(80, docs_width)

    def format(string):
        text = re.sub(r'\s+', ' ', string)
        text = re.sub(r'\s*\\n\s*', '\n', text)
        paras = text.split('\n')
        paras = [textwrap.fill(p, width=docs_width) for p in paras]
        return '\n'.join(paras)

    def formatter(prog):
        return argparse.RawDescriptionHelpFormatter(prog, width=docs_width)

    def size2order(human):
        units = {
            "K": 2**10, "M": 2**20, "G": 2**30,
            "k": 2**10, "m": 2**20, "g": 2**30,
        }
        unit = 1
        if human[-1] in units:
            unit = units[human[-1]]
            human = human[:-1]
        try:
            size = int(human)
        except ValueError:
            raise ArgException('error: --cont value must be integer size with optional KMG unit')
        size *= unit
        order = int(math.log2(size / PAGE_SIZE))
        if order < 1:
            raise ArgException('error: --cont value must be size of at least 2 pages')
        if (1 << order) * PAGE_SIZE != size:
            raise ArgException('error: --cont value must be size of power-of-2 pages')
        if order > PMD_ORDER:
            raise ArgException('error: --cont value must be less than or equal to PMD order')
        return order

    parser = argparse.ArgumentParser(formatter_class=formatter,
        description=format("""Prints information about how transparent huge
                    pages are mapped, either system-wide, or for a specified
                    process or cgroup.\\n
                    \\n
                    When run with --pid, the user explicitly specifies the set
                    of pids to scan. e.g. "--pid 10 [--pid 134 ...]". When run
                    with --cgroup, the user passes either a v1 or v2 cgroup and
                    all pids that belong to the cgroup subtree are scanned. When
                    run with neither --pid nor --cgroup, the full set of pids on
                    the system is gathered from /proc and scanned as if the user
                    had provided "--pid 1 --pid 2 ...".\\n
                    \\n
                    A default set of statistics is always generated for THP
                    mappings. However, it is also possible to generate
                    additional statistics for "contiguous block mappings" where
                    the block size is user-defined.\\n
                    \\n
                    Statistics are maintained independently for anonymous and
                    file-backed (pagecache) memory and are shown both in kB and
                    as a percentage of either total anonymous or total
                    file-backed memory as appropriate.\\n
                    \\n
                    THP Statistics\\n
                    --------------\\n
                    \\n
                    Statistics are always generated for fully- and
                    contiguously-mapped THPs whose mapping address is aligned to
                    their size, for each <size> supported by the system.
                    Separate counters describe THPs mapped by PTE vs those
                    mapped by PMD. (Although note a THP can only be mapped by
                    PMD if it is PMD-sized):\\n
                    \\n
                    - anon-thp-pte-aligned-<size>kB\\n
                    - file-thp-pte-aligned-<size>kB\\n
                    - anon-thp-pmd-aligned-<size>kB\\n
                    - file-thp-pmd-aligned-<size>kB\\n
                    \\n
                    Similarly, statistics are always generated for fully- and
                    contiguously-mapped THPs whose mapping address is *not*
                    aligned to their size, for each <size> supported by the
                    system. Due to the unaligned mapping, it is impossible to
                    map by PMD, so there are only PTE counters for this case:\\n
                    \\n
                    - anon-thp-pte-unaligned-<size>kB\\n
                    - file-thp-pte-unaligned-<size>kB\\n
                    \\n
                    Statistics are also always generated for mapped pages that
                    belong to a THP but where the is THP is *not* fully- and
                    contiguously- mapped. These "partial" mappings are all
                    counted in the same counter regardless of the size of the
                    THP that is partially mapped:\\n
                    \\n
                    - anon-thp-pte-partial\\n
                    - file-thp-pte-partial\\n
                    \\n
                    Contiguous Block Statistics\\n
                    ---------------------------\\n
                    \\n
                    An optional, additional set of statistics is generated for
                    every contiguous block size specified with `--cont <size>`.
                    These statistics show how much memory is mapped in
                    contiguous blocks of <size> and also aligned to <size>. A
                    given contiguous block must all belong to the same THP, but
                    there is no requirement for it to be the *whole* THP.
                    Separate counters describe contiguous blocks mapped by PTE
                    vs those mapped by PMD:\\n
                    \\n
                    - anon-cont-pte-aligned-<size>kB\\n
                    - file-cont-pte-aligned-<size>kB\\n
                    - anon-cont-pmd-aligned-<size>kB\\n
                    - file-cont-pmd-aligned-<size>kB\\n
                    \\n
                    As an example, if monitoring 64K contiguous blocks (--cont
                    64K), there are a number of sources that could provide such
                    blocks: a fully- and contiguously-mapped 64K THP that is
                    aligned to a 64K boundary would provide 1 block. A fully-
                    and contiguously-mapped 128K THP that is aligned to at least
                    a 64K boundary would provide 2 blocks. Or a 128K THP that
                    maps its first 100K, but contiguously and starting at a 64K
                    boundary would provide 1 block. A fully- and
                    contiguously-mapped 2M THP would provide 32 blocks. There
                    are many other possible permutations.\\n"""),
        epilog=format("""Requires root privilege to access pagemap and
                    kpageflags."""))

    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('--pid',
        metavar='pid', required=False, type=int, default=[], action='append',
        help="""Process id of the target process. Maybe issued multiple times to
            scan multiple processes. --pid and --cgroup are mutually exclusive.
            If neither are provided, all processes are scanned to provide
            system-wide information.""")

    group.add_argument('--cgroup',
        metavar='path', required=False,
        help="""Path to the target cgroup in sysfs. Iterates over every pid in
            the cgroup and its children. --pid and --cgroup are mutually
            exclusive. If neither are provided, all processes are scanned to
            provide system-wide information.""")

    parser.add_argument('--rollup',
        required=False, default=False, action='store_true',
        help="""Sum the per-vma statistics to provide a summary over the whole
            system, process or cgroup.""")

    parser.add_argument('--cont',
        metavar='size[KMG]', required=False, default=[], action='append',
        help="""Adds stats for memory that is mapped in contiguous blocks of
            <size> and also aligned to <size>. May be issued multiple times to
            track multiple sized blocks. Useful to infer e.g. arm64 contpte and
            hpa mappings. Size must be a power-of-2 number of pages.""")

    parser.add_argument('--inc-smaps',
        required=False, default=False, action='store_true',
        help="""Include all numerical, additive /proc/<pid>/smaps stats in the
            output.""")

    parser.add_argument('--inc-empty',
        required=False, default=False, action='store_true',
        help="""Show all statistics including those whose value is 0.""")

    parser.add_argument('--periodic',
        metavar='sleep_ms', required=False, type=int,
        help="""Run in a loop, polling every sleep_ms milliseconds.""")

    args = parser.parse_args()

    try:
        args.cont = [size2order(cont) for cont in args.cont]
    except ArgException as e:
        parser.print_usage()
        raise

    if args.periodic:
        while True:
            do_main(args)
            print()
            time.sleep(args.periodic / 1000)
    else:
        do_main(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        prog = os.path.basename(sys.argv[0])
        print(f'{prog}: {e}')
        exit(1)
