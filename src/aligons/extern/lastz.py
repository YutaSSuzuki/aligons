"""Pairwise genome alignment.

src: {ensemblgenomes.prefix}/fasta/{species}/*.fa.gz
dst: ./pairwise/{target}/{query}/{chromosome}/sing.maf

https://lastz.github.io/lastz/
"""
import concurrent.futures as confu
import gzip
import logging
from pathlib import Path
from types import MappingProxyType

from aligons.db import ensemblgenomes, phylo
from aligons.util import ConfDict, cli, config, fs, read_config, subp

from . import kent

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None):
    parser = cli.ArgumentParser()
    parser.add_argument("-c", "--config", type=Path)
    parser.add_argument("target", choices=ensemblgenomes.species_names())
    parser.add_argument("query", nargs="*")
    args = parser.parse_args(argv or None)
    if args.config:
        read_config(args.config)
    _run(args.target, args.query)


def run(target: str, clade: str):
    tree = phylo.newicks[clade]
    _run(target, phylo.extract_names(tree))
    return Path("pairwise") / target


def _run(target: str, queries: list[str]):
    queries = ensemblgenomes.sanitize_queries(target, queries)
    futures: list[confu.Future[Path]] = []
    for query in queries:
        pa = PairwiseAlignment(target, query, config)
        futures.extend(pa.run())
    cli.wait_raise(futures)


class PairwiseAlignment:
    def __init__(self, target: str, query: str, options: ConfDict):
        self._target = target
        self._query = query
        self._target_sizes = ensemblgenomes.get_file("fasize.chrom.sizes", target)
        self._query_sizes = ensemblgenomes.get_file("fasize.chrom.sizes", query)
        self._outdir = Path("pairwise") / target / query
        self._lastz_opts: ConfDict = options["lastz"]
        self._axtch_opts: ConfDict = options["axtChain"]
        self._cn_opts: ConfDict = options["chainNet"]
        self._toaxt_opts: ConfDict = options["netToAxt"]

    def run(self):
        pool = cli.ThreadPool()
        if not cli.dry_run:
            self._outdir.mkdir(0o755, parents=True, exist_ok=True)
        patt = "*.chromosome.*.2bit"
        subdir = "kmer" if config["db"]["kmer"] else ""
        it = ensemblgenomes.glob(patt, [self._target], subdir)
        target_chromosomes = fs.sorted_naturally(it)
        it = ensemblgenomes.glob(patt, [self._query], subdir)
        query_chromosomes = fs.sorted_naturally(it)
        flists: list[list[confu.Future[Path]]] = []
        for t in target_chromosomes:
            flists.append(
                [pool.submit(self.align_chr, t, q) for q in query_chromosomes]
            )
        return [pool.submit(self.wait_integrate, futures) for futures in flists]

    def align_chr(self, t2bit: Path, q2bit: Path):
        axtgz = lastz(t2bit, q2bit, self._outdir, self._lastz_opts)
        return kent.axt_chain(t2bit, q2bit, axtgz, self._axtch_opts)

    def wait_integrate(self, futures: list[confu.Future[Path]]):
        return self.integrate([f.result() for f in futures])

    def integrate(self, chains: list[Path]):
        pre_chain = kent.merge_sort_pre(chains, self._target_sizes, self._query_sizes)
        syntenic_net = kent.chain_net_syntenic(
            pre_chain, self._target_sizes, self._query_sizes, self._cn_opts
        )
        sing_maf = kent.net_axt_maf(
            syntenic_net, pre_chain, self._target, self._query, self._toaxt_opts
        )
        if sing_maf.exists():
            print(sing_maf)
        return sing_maf


def lastz(
    t2bit: Path, q2bit: Path, outdir: Path, options: ConfDict = MappingProxyType({})
):
    target_label = t2bit.stem.rsplit("dna_sm.", 1)[1]
    query_label = q2bit.stem.rsplit("dna_sm.", 1)[1]
    subdir = outdir / target_label
    if not cli.dry_run:
        subdir.mkdir(0o755, exist_ok=True)
    axtgz = subdir / f"{query_label}.axt.gz"
    cmd = f"lastz {t2bit} {q2bit} --format=axt"
    cmd += subp.optjoin(options)
    is_to_run = fs.is_outdated(axtgz, [t2bit, q2bit])
    lastz = subp.run_if(is_to_run, cmd, stdout=subp.PIPE)
    if is_to_run and not cli.dry_run:
        with gzip.open(axtgz, "wb") as fout:
            fout.write(lastz.stdout)
    return axtgz


if __name__ == "__main__":
    main()
