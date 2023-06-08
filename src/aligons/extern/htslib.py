import concurrent.futures as confu
import gzip
import logging
import re
from collections.abc import Iterable
from pathlib import Path

from aligons.util import cli, fs, subp

_log = logging.getLogger(__name__)


def create_genome_bgzip(path: Path):
    """Combine chromosome files and bgzip it."""
    if (ext := path.parent.name) != "gff3":
        ext = "fa"
    files = fs.sorted_naturally(path.glob(rf"*.chromosome.*.{ext}.gz"))
    assert files
    _log.debug(str(files))
    name = files[0].name
    (outname, count) = re.subn(rf"\.chromosome\..+\.{ext}", rf".genome.{ext}", name)
    assert count == 1
    outfile = path / outname
    return concat_bgzip(files, outfile)


def concat_bgzip(infiles: list[Path], outfile: Path):
    if fs.is_outdated(outfile, infiles) and not cli.dry_run:
        with outfile.open("wb") as fout:
            bgzip = subp.popen("bgzip -@2", stdin=subp.PIPE, stdout=fout)
            assert bgzip.stdin
            if ".gff" in outfile.name:
                header = collect_gff3_header(infiles)
                bgzip.stdin.write(header)
                bgzip.stdin.flush()
                _log.debug(header.decode())
            for file in infiles:
                if ".gff" in outfile.name:
                    p = sort_clean_chromosome_gff3(file)
                    (stdout, _stderr) = p.communicate()
                    bgzip.stdin.write(stdout)
                    bgzip.stdin.flush()
                else:
                    with gzip.open(file, "rb") as fin:
                        bgzip.stdin.write(fin.read())
                        bgzip.stdin.flush()
            bgzip.communicate()
    _log.info(f"{outfile}")
    return outfile


def collect_gff3_header(infiles: Iterable[Path]):
    header = b"##gff-version 3\n"
    for file in infiles:
        with gzip.open(file, "rt") as fin:
            for line in fin:
                if line.startswith("##sequence-region"):
                    header += line.encode()
                    break
                if not line.startswith("#"):
                    break
    return header


def bgzip(path: Path):
    """https://www.htslib.org/doc/bgzip.html."""
    outfile = path.with_suffix(path.suffix + ".gz")
    subp.run_if(fs.is_outdated(outfile, path), ["bgzip", "-@2", path])
    return outfile


def bgzip_compress(data: bytes) -> bytes:
    return subp.run(["bgzip", "-@2"], input=data, stdout=subp.PIPE).stdout


def try_index(bgz: Path | cli.FuturePath) -> Path:
    if isinstance(bgz, confu.Future):
        bgz = bgz.result()
    if to_be_tabixed(bgz.name):
        return tabix(bgz)
    if to_be_faidxed(bgz.name):
        return faidx(bgz)
    return bgz


def faidx(bgz: Path | cli.FuturePath):
    """https://www.htslib.org/doc/samtools-faidx.html."""
    if isinstance(bgz, confu.Future):
        bgz = bgz.result()
    outfile = bgz.with_suffix(bgz.suffix + ".fai")
    subp.run_if(fs.is_outdated(outfile, bgz), ["samtools", "faidx", bgz])
    _log.info(f"{outfile}")
    return outfile


def tabix(bgz: Path | cli.FuturePath):
    """https://www.htslib.org/doc/tabix.html.

    Use .csi instead of .tbi for chromosomes >512 Mbp e.g., atau, hvul.
    """
    if isinstance(bgz, confu.Future):
        bgz = bgz.result()
    outfile = bgz.with_suffix(bgz.suffix + ".csi")
    subp.run_if(fs.is_outdated(outfile, bgz), ["tabix", "--csi", bgz])
    _log.info(f"{outfile}")
    return outfile


def to_be_bgzipped(filename: str):
    return to_be_faidxed(filename) or to_be_tabixed(filename)


def to_be_faidxed(filename: str):
    ext = (".fa", ".fas", ".fasta", ".fna")
    return filename.removesuffix(".gz").removesuffix(".zip").endswith(ext)


def to_be_tabixed(filename: str):
    ext = (".gff", ".gff3", ".gtf", ".bed")
    return filename.removesuffix(".gz").removesuffix(".zip").endswith(ext)


def sort_clean_chromosome_gff3(infile: Path):
    # TODO: jbrowse2 still needs billzt/gff3sort precision?
    p1 = subp.popen(f"zgrep -v '^#' {infile!s}", stdout=subp.PIPE, quiet=True)
    p2 = subp.popen(
        "grep -v '\tchromosome\t'", stdin=p1.stdout, stdout=subp.PIPE, quiet=True
    )
    if p1.stdout:
        p1.stdout.close()
    p3 = subp.popen("sort -k4,4n", stdin=p2.stdout, stdout=subp.PIPE, quiet=True)
    if p2.stdout:
        p2.stdout.close()
    return p3
