"""Convert MAF to SAM/BAM/CRAM for visualzation.

src: ./pairwise/{target}/{query}/{chromosome}/sing.maf
dst: ./pairwise/{target}/{query}/cram/genome.cram
"""
import concurrent.futures as confu
import logging
import re
from pathlib import Path

from aligons.db import api
from aligons.util import cli, fs, subp

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None):
    parser = cli.ArgumentParser()
    parser.add_argument("-t", "--test", action="store_true")
    parser.add_argument("query", nargs="*", type=Path)  # pairwise/{target}/{query}
    args = parser.parse_args(argv or None)
    if args.test:
        for path in args.query:
            target = path.parent.parent.parent.name
            reference = api.genome_fa(target)
            stem = str(path.parent).replace("/", "_")
            maf2cram(path, Path(stem + ".cram"), reference)
        return
    cli.wait_raise([mafs2cram(path) for path in args.query])


def run(target: Path, species: list[str]):
    query_names = api.sanitize_queries(target.name, species)
    return [mafs2cram(target / q) for q in query_names]


def mafs2cram(path: Path):
    target_species = path.parent.name
    reference = api.genome_fa(target_species)
    outdir = path / "cram"
    if not cli.dry_run:
        outdir.mkdir(0o755, exist_ok=True)
    pool = cli.ThreadPool()
    futures: list[confu.Future[Path]] = []
    for chr_dir in fs.sorted_naturally(path.glob("chromosome.*")):
        maf = chr_dir / "sing.maf"
        if not maf.exists():
            _log.warning(f"not found {maf}")
            continue
        cram = outdir / (chr_dir.name + ".cram")
        futures.append(pool.submit(maf2cram, maf, cram, reference))
    return pool.submit(merge_crams, futures, outdir)


def merge_crams(futures: list[confu.Future[Path]], outdir: Path):
    crams: list[Path] = [f.result() for f in futures]
    outfile = outdir / "genome.cram"
    is_to_run = bool(crams) and fs.is_outdated(outfile, crams)
    cmd = f"samtools merge --no-PG -O CRAM -@ 2 -f -o {outfile!s} "
    cmd += " ".join([str(x) for x in crams])
    subp.run(cmd, if_=is_to_run)
    subp.run(["samtools", "index", outfile], if_=is_to_run)
    if outfile.exists():
        print(outfile)
    return outfile


def maf2cram(infile: Path, outfile: Path, reference: Path):
    is_to_run = fs.is_outdated(outfile, infile)
    mafconv = subp.popen(
        ["maf-convert", "sam", infile], if_=is_to_run, stdout=subp.PIPE
    )
    (stdout, _stderr) = mafconv.communicate()
    content = sanitize_cram(reference, stdout, if_=is_to_run)
    cmd = f"samtools sort --no-PG -O CRAM -@ 2 -o {outfile!s}"
    subp.popen(cmd, if_=is_to_run, stdin=subp.PIPE).communicate(content)
    _log.info(f"{outfile}")
    return outfile


def sanitize_cram(reference: Path, sam: bytes, *, if_: bool):
    def repl(mobj: re.Match[bytes]):
        qstart = 0
        if int(mobj["flag"]) & 16:  # reverse strand
            if tail := mobj["tail_cigar"]:
                qstart = int(tail.rstrip(b"H")) + 1
        elif head := mobj["head_cigar"]:
            qstart = int(head.rstrip(b"H")) + 1
        qend = qstart + len(mobj["seq"]) - 1
        cells = [
            mobj["qname"] + f":{qstart}-{qend}".encode(),
            mobj["flag"],
            mobj["rname"],
            mobj["pos"],
            mobj["mapq"],
            mobj["cigar"],
            mobj["rnext"],
            mobj["pnext"],
            mobj["tlen"],
            mobj["seq"],
            mobj["misc"],
        ]
        return b"\t".join(cells)

    patt = re.compile(
        rb"^(?P<qname>\S+)\t(?P<flag>\d+)\t"
        rb"\w+\.(?P<rname>\S+)\t(?P<pos>\d+)\t(?P<mapq>\d+)\t"
        rb"(?P<head_cigar>\d+H)?(?P<cigar>\S+?)(?P<tail_cigar>\d+H)?\t"
        rb"(?P<rnext>\S+)\t(?P<pnext>\w+)\t(?P<tlen>\d+)\t"
        rb"(?P<seq>\w+)\t(?P<misc>.+$)"
    )
    lines = [patt.sub(repl, line) for line in sam.splitlines(keepends=True)]
    cmd = f"samtools view --no-PG -h -C -@ 2 -T {reference!s}"
    samview = subp.popen(cmd, if_=if_, stdin=subp.PIPE, stdout=subp.PIPE)
    (stdout, _stderr) = samview.communicate(b"".join(lines))
    return stdout


if __name__ == "__main__":
    main()
