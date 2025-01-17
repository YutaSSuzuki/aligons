import gzip
import io
import logging
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import polars as pl

from aligons import db
from aligons.extern import htslib, jellyfish, kent
from aligons.util import cli, fs

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None):
    parser = cli.ArgumentParser()
    parser.add_argument("infile", type=Path)
    args = parser.parse_args(argv or None)
    split_gff(args.infile)


def index_fasta(paths: list[Path]):
    """Create bgzipped and indexed genome.fa."""
    if len(paths) == 1:
        paths = [f.result() for f in _split_toplevel_fa(paths[0])]
    fts = [cli.thread_submit(kent.faToTwoBit, x) for x in paths]
    genome = _create_genome_bgzip(paths)
    htslib.faidx(genome)
    kent.faToTwoBit(genome)
    kent.faSize(genome)
    cli.wait_raise(fts)
    return genome


def index_gff3(paths: list[Path]):  # gff3/{species}
    """Create bgzipped and indexed genome.gff3."""
    if len(paths) == 1:
        assert "chromosome" not in paths[0].name, paths[0]
        paths = split_gff(paths[0])
    genome = _create_genome_bgzip(paths)
    htslib.tabix(genome)
    return genome


def _create_genome_bgzip(files: list[Path]):
    """Combine chromosome files and bgzip it."""
    files = fs.sorted_naturally(files)
    _log.debug(str(files))
    if cli.dry_run and not files:
        return Path("/dev/null")
    name = files[0].name
    ext = files[0].with_suffix("").suffix
    (outname, count) = re.subn(rf"\.chromosome\..+{ext}", rf".genome{ext}", name)
    assert count == 1, name
    outfile = files[0].parent / outname
    return htslib.concat_bgzip(files, outfile)


def _split_toplevel_fa(fa_gz: Path) -> list[cli.FuturePath]:
    assert "toplevel" in fa_gz.name, fa_gz
    fmt = "{stem}.{seqid}.fa.gz"
    return htslib.split_fa_gz(fa_gz, fmt, (r"toplevel", "chromosome"))


def softmask(species: str):
    masked = jellyfish.run(species)
    return index_fasta(masked)


def compress(content: bytes, outfile: Path) -> Path:
    """Uncompress/compress depending on file names.

    - .gff |> uncompress |> sort |> bgzip
    - .bed |> uncompress |> bgzip
    - .fa |> uncompress |> bgzip
    - .zip |> uncompress
    - gzip if outfile has new .gz
    """
    if not cli.dry_run and fs.is_outdated(outfile):
        if fs.is_zip(content):
            assert outfile.suffix != ".zip", outfile
            content = fs.zip_decompress(content)
        if htslib.to_be_bgzipped(outfile.name):
            assert outfile.suffix == ".gz", outfile
            content = fs.gzip_decompress(content)
            if ".gff" in outfile.name:
                content = sort_gff(content)
            content = htslib.bgzip_compress(content)
        elif outfile.suffix == ".gz":
            content = fs.gzip_compress(content)
        outfile.parent.mkdir(0o755, parents=True, exist_ok=True)
        with outfile.open("wb") as fout:
            fout.write(content)
    _log.info(f"{outfile}")
    return outfile


def retrieve_content(
    url: str, outfile: Path | None = None, *, force: bool = False
) -> bytes:
    _log.debug(url)
    if outfile is None:
        urlp = urlparse(url)
        outfile = db.path_mirror(urlp.netloc + urlp.path)
    if cli.dry_run and not force:
        content = b""
    elif fs.is_outdated(outfile):
        outfile.parent.mkdir(0o755, parents=True, exist_ok=True)
        response = urllib.request.urlopen(url)  # noqa: S310
        content = response.read()
        with outfile.open("wb") as fout:
            fout.write(content)
    else:
        with outfile.open("rb") as fin:
            content = fin.read()
    _log.info(f"{outfile}")
    return content


def split_gff(path: Path):
    regions = read_gff_sequence_region(path)
    body = read_gff_body(path)
    stem = path.stem.removesuffix(".gff").removesuffix(".gff3")
    files: list[Path] = []
    _log.debug(f"{stem=}")
    for name, data in body.groupby("seqid", maintain_order=True):
        seqid = str(name)
        if seqid.startswith("scaffold"):
            _log.debug(f"ignoring scaffold: {seqid}")
            continue
        outfile = path.parent / f"{stem}.chromosome.{seqid}.gff3.gz"
        files.append(outfile)
        _log.info(f"{outfile}")
        if cli.dry_run or not fs.is_outdated(outfile, path):
            continue
        with gzip.open(outfile, "wt") as fout:
            fout.write("##gff-version 3\n")
            fout.write(regions.get(seqid, ""))
            fout.write(data.sort(["start"]).write_csv(has_header=False, separator="\t"))
    return files


def read_gff_sequence_region(path: Path) -> dict[str, str]:
    lines: list[str] = []
    with gzip.open(path, "rt") as fin:
        for line in fin:
            if not line.startswith("#"):
                break
            lines.append(line)
    if not lines or not lines[0].startswith("##gff-version"):
        _log.warning(f"{path}:invalid GFF without ##gff-version")
    else:
        lines.pop(0)
    regions: dict[str, str] = {}
    comments: list[str] = []
    # solgenomics has dirty headers without space: ##sequence-regionSL4.0ch01
    pattern = re.compile(r"(##sequence-region)\s*(.+)", re.S)
    for line in lines:
        if mobj := pattern.match(line):
            value = mobj.group(2)
            regions[value.split()[0]] = " ".join(mobj.groups())
        else:
            comments.append(line)
    if not regions:
        _log.info(f"{path}:unfriendly GFF without ##sequence-region")
    if comments:
        ignored = "\n".join(comments)
        _log.warning(f"{path}:comments in GFF ignored:\n{ignored}")
    return regions


def sort_gff(content: bytes) -> bytes:
    return extract_gff_header(content) + sort_gff_body(content)


def extract_gff_header(content: bytes) -> bytes:
    if m := re.match(rb"##gff-version.+?(?=^[^#])", content, re.M | re.S):
        return m.group(0)
    _log.warning("invalid GFF without ##gff-version")
    return b"##gff-version 3\n"


def sort_gff_body(content: bytes) -> bytes:
    bio = io.BytesIO()
    (
        read_gff_body(content)
        .sort(["seqid", "start"])
        .write_csv(bio, has_header=False, separator="\t")
    )
    return bio.getvalue()


def read_gff_body(source: Path | str | bytes):
    if isinstance(source, bytes):
        source = re.sub(rb"\n\n+", rb"\n", source)
    return pl.read_csv(
        source,
        separator="\t",
        comment_char="#",
        has_header=False,
        dtypes=[pl.Utf8],
        new_columns=[
            "seqid",
            "source",
            "type",
            "start",
            "end",
            "score",
            "strand",
            "phase",
            "attributes",
        ],
    )


if __name__ == "__main__":
    main()
