"""https://plants.ensembl.org/."""
import functools
import logging
import os
import re
from collections.abc import Iterable
from contextlib import suppress
from ftplib import FTP
from pathlib import Path

from aligons import db
from aligons.db import tools
from aligons.extern import htslib
from aligons.util import cli, config, fs, subp

_log = logging.getLogger(__name__)


def main(argv: list[str] | None = None):
    parser = cli.ArgumentParser()
    parser.add_argument("-V", "--versions", action="store_true")
    parser.add_argument("-a", "--all", action="store_true")
    parser.add_argument("-f", "--files", action="store_true")
    parser.add_argument("-g", "--glob", default="*")
    parser.add_argument("--name", action="store_true")
    parser.add_argument("species", nargs="*")
    args = parser.parse_args(argv or None)
    if args.versions:
        for x in sorted(list_versions()):
            print(x)
        return
    species = species_names_all() if args.all and not args.files else species_names()
    if args.species:
        species = list(filter_by_shortname(species, args.species))
    if not args.files:
        for sp in species:
            print(sp)
        return
    for x in glob(args.glob, species):
        if args.name:
            print(x.name)
        else:
            print(x)


def make_newicks():
    ehrhartoideae = "(oryza_sativa,leersia_perrieri)ehrhartoideae"
    pooideae = "(brachypodium_distachyon,(aegilops_tauschii,hordeum_vulgare))pooideae"
    andropogoneae = "(sorghum_bicolor,zea_mays)andropogoneae"
    paniceae = "(setaria_italica,panicum_hallii_fil2)paniceae"
    bep = f"({ehrhartoideae},{pooideae})bep"
    pacmad = f"({andropogoneae},{paniceae})pacmad"
    poaceae = f"({bep},{pacmad})poaceae"
    monocot = f"(({poaceae},musa_acuminata),dioscorea_rotundata)monocot"

    _solanum = "(solanum_lycopersicum,solanum_tuberosum)"
    solanaceae = f"(({_solanum},capsicum_annuum),nicotiana_attenuata)solanaceae"
    _convolvulaceae = "ipomoea_triloba"
    solanales = f"({solanaceae},{_convolvulaceae})solanales"
    _lamiales = "olea_europaea_sylvestris"
    if version() > 51:  # noqa: PLR2004
        _lamiales = f"({_lamiales},sesamum_indicum)"
    lamiids = f"(({solanales},coffea_canephora),{_lamiales})lamiids"
    _asteraceae = "helianthus_annuus"
    if version() > 52:  # noqa: PLR2004
        _asteraceae = f"({_asteraceae},lactuca_sativa)"
    _companulids = f"({_asteraceae},daucus_carota)"
    _core_asterids = f"({lamiids},{_companulids})"
    asterids = f"({_core_asterids},actinidia_chinensis)asterids"
    eudicots = f"({asterids},arabidopsis_thaliana)eudicots"

    angiospermae = f"({eudicots},{monocot})angiospermae"
    assert "oryza_sativa" in angiospermae
    return {k: v + ";" for k, v in locals().items() if not k.startswith("_")}


def list_versions():
    _log.debug(f"{local_db_root()=}")
    return local_db_root().glob("release-*")


@functools.cache
def species_names_all():
    with FTPensemblgenomes() as ftp:
        lst = ftp.nlst_cache("fasta")
    return [Path(x).name for x in lst]


@functools.cache
def species_names(fmt: str = "fasta"):
    return [x.name for x in species_dirs(fmt)]


def species_dirs(fmt: str = "fasta", species: list[str] | None = None):
    assert (root := prefix() / fmt).exists(), root
    requests = set(species or [])
    for path in root.iterdir():
        if not path.is_dir():
            continue
        if not species or (path.name in requests):
            requests.discard(path.name)  # TODO: search twice
            yield path
    assert not requests, f"directory not found: {requests}"


def get_file(pattern: str, species: str, subdir: str = ""):
    found = list(glob(pattern, [species], subdir))
    _log.debug(f"{found=}")
    assert len(found) == 1
    return found[0]


def glob(pattern: str, species: list[str], subdir: str = ""):
    for path in species_dirs("fasta", species):
        for x in fs.sorted_naturally((path / "dna" / subdir).glob(pattern)):
            yield x
    for path in species_dirs("gff3", species):
        for x in fs.sorted_naturally(path.glob(pattern)):
            yield x


def expand_shortnames(shortnames: list[str]):
    return filter_by_shortname(species_names_all(), shortnames)


def filter_by_shortname(species: Iterable[str], queries: Iterable[str]):
    return (x for x in species if shorten(x) in queries)


def shorten(name: str):
    """Oryza_sativa -> osat."""
    if name.lower() == "olea_europaea_sylvestris":
        return "oesy"
    split = name.lower().split("_")
    return split[0][0] + split[1][:3]


def sanitize_queries(target: str, queries: list[str]):
    queries = list(dict.fromkeys(queries))
    with suppress(ValueError):
        queries.remove(target)
    assert queries
    _log.debug(f"{queries=}")
    assert set(queries) <= set(species_names())
    return queries


def consolidate_compara_mafs(indir: Path):
    _log.debug(f"{indir=}")
    mobj = re.search(r"([^_]+)_.+?\.v\.([^_]+)", indir.name)
    assert mobj
    target_short = mobj.group(1)
    query_short = mobj.group(2)
    target = list(expand_shortnames([target_short]))[0]
    query = list(expand_shortnames([query_short]))[0]
    outdir = Path("compara") / target / query
    pat = re.compile(r"lastz_net\.([^_]+)_\d+\.maf$")
    infiles_by_seq: dict[str, list[Path]] = {}
    for maf in fs.sorted_naturally(indir.glob("*_*.maf")):
        mobj = pat.search(maf.name)
        assert mobj
        seq = mobj.group(1)
        infiles_by_seq.setdefault(seq, []).append(maf)
    for seq, infiles in infiles_by_seq.items():
        if seq == "supercontig":
            continue
        chrdir = outdir / f"chromosome.{seq}"
        chrdir.mkdir(0o755, parents=True, exist_ok=True)
        sing_maf = chrdir / "sing.maf"
        _log.info(str(sing_maf))
        if not fs.is_outdated(sing_maf, infiles):
            continue
        lines: list[str] = ["##maf version=1 scoring=LASTZ_NET\n"]
        for maf in infiles:
            lines.extend(readlines_compara_maf(maf))
        with sing_maf.open("wb") as fout:
            cmd = f"sed -e 's/{target}/{target_short}/' -e 's/{query}/{query_short}/'"
            sed = subp.popen(cmd, stdin=subp.PIPE, stdout=subp.PIPE)
            maff = subp.popen("mafFilter stdin", stdin=sed.stdout, stdout=fout)
            # for padding, not for filtering
            assert sed.stdout
            sed.stdout.close()
            sed.communicate("".join(lines).encode())
            maff.communicate()
    _log.info(f"{outdir}")
    return outdir


def readlines_compara_maf(file: Path):
    """MAF files of ensembl compara have broken "a" lines.

    a# id: 0000000
     score=9999
    s aaa.1
    s bbb.1
    """
    with file.open("r") as fin:
        for line in fin:
            if line.startswith(("#", "a#")):
                continue
            if line.startswith(" score"):
                yield "a" + line
            else:
                yield line


class FTPensemblgenomes(FTP):
    def __init__(self):
        _log.info("FTP()")
        super().__init__()

    def quit(self):  # noqa: A003
        _log.info(f"os.chdir({self.orig_wd})")
        os.chdir(self.orig_wd)
        _log.info("ftp.quit()")
        resp = super().quit()
        _log.info(resp)
        return resp

    def lazy_init(self):
        if self.sock is not None:
            return
        host = "ftp.ensemblgenomes.org"
        _log.debug(f"ftp.connect({host})")
        _log.info(self.connect(host))
        _log.debug("ftp.login()")
        _log.info(self.login())
        path = f"/pub/plants/release-{version()}"
        _log.info(f"ftp.cwd({path})")
        _log.info(self.cwd(path))
        _log.info(f"os.chdir({prefix()})")
        self.orig_wd = Path.cwd()
        prefix().mkdir(0o755, parents=True, exist_ok=True)
        os.chdir(prefix())  # for RETR only

    def download_fasta(self, species: str):
        relpath = f"fasta/{species}/dna"
        outdir = prefix() / relpath
        nlst = self.nlst_cache(relpath)
        for x in self.remove_duplicates(nlst, "_sm."):
            outfile = self.retrieve(x)
            post_retrieval(outfile)
        fs.checksums(outdir / "CHECKSUMS")
        return outdir

    def download_gff3(self, species: str):
        relpath = f"gff3/{species}"
        outdir = prefix() / relpath
        nlst = self.nlst_cache(relpath)
        for x in self.remove_duplicates(nlst):
            outfile = self.retrieve(x)
            post_retrieval(outfile)
        fs.checksums(outdir / "CHECKSUMS")
        return outdir

    def download_maf(self, species: str):
        relpath = "maf/ensembl-compara/pairwise_alignments"
        outdir = prefix() / relpath
        nlst = self.nlst_cache(relpath)
        sp = shorten(species)
        for x in nlst:
            if f"/{sp}_" in x:
                self.retrieve(x)
        _log.debug(f"{outdir=}")
        dirs: list[Path] = []
        for targz in outdir.glob("*.tar.gz"):
            expanded = prefix() / targz.with_suffix("").with_suffix("")
            tar = ["tar", "xzf", targz, "-C", outdir]
            subp.run_if(fs.is_outdated(expanded / "README.maf"), tar)
            # TODO: MD5SUM
            dirs.append(expanded.resolve())
        return dirs

    def remove_duplicates(self, nlst: list[str], substr: str = ""):
        matched = [x for x in nlst if "chromosome" in x]
        if not matched:
            matched = [x for x in nlst if "primary_assembly" in x]
        if not matched:
            matched = [x for x in nlst if "toplevel" in x]
        if not matched:
            matched = [x for x in nlst if f"{version()}.gff3" in x]
        matched = [x for x in matched if "musa_acuminata_v2" not in x]  # v52
        if substr:
            matched = [x for x in matched if substr in x]
        assert matched
        misc = [x for x in nlst if re.search("CHECKSUMS$|README$", x)]
        return matched + misc

    def nlst_cache(self, relpath: str):
        cache = prefix() / relpath / ".ftp_nlst_cache"
        if cache.exists():
            _log.info(f"{cache=}")
            with cache.open("r") as fin:
                names = fin.read().rstrip().splitlines()
            lst = [str(Path(relpath) / x) for x in names]
        else:
            self.lazy_init()
            _log.info(f"ftp.nlst({relpath})")
            lst = self.nlst(relpath)  # ensembl does not support mlsd
            cache.parent.mkdir(0o755, parents=True, exist_ok=True)
            with cache.open("w") as fout:
                fout.write("\n".join([Path(x).name for x in lst]) + "\n")
        return lst

    def retrieve(self, path: str):
        outfile = prefix() / path
        if not outfile.exists() and not cli.dry_run:
            outfile.parent.mkdir(0o755, parents=True, exist_ok=True)
            with outfile.open("wb") as fout:
                cmd = f"RETR {path}"
                self.lazy_init()
                _log.info(f"ftp.retrbinary({cmd})")
                _log.info(self.retrbinary(cmd, fout.write))
        _log.info(f"{outfile}")
        return outfile


def post_retrieval(outfile: Path):
    if "primary_assembly" in outfile.name:
        link = Path(str(outfile).replace("primary_assembly", "chromosome"))
        if outfile.exists() and not link.exists():
            link.symlink_to(outfile)
    elif "toplevel" in outfile.name:
        if outfile.name.endswith(".fa.gz"):
            split_toplevel_fa(outfile)
        elif outfile.name.endswith(".gff3.gz"):
            tools.split_gff(outfile)


def split_toplevel_fa(fa_gz: Path):
    fmt = "{stem}.{seqid}.fa.gz"
    return htslib.split_fa_gz(fa_gz, fmt, (r"toplevel", "chromosome"))


def rsync(relpath: str, options: str = ""):
    server = "ftp.ensemblgenomes.org"
    remote_prefix = f"rsync://{server}/all/pub/plants/release-{version()}"
    src = f"{remote_prefix}/{relpath}/"
    dst = prefix() / relpath
    return subp.run(f"rsync -auv {options} {src} {dst}")


def prefix():
    return local_db_root() / f"release-{version()}"


def local_db_root():
    return db.path("ensemblgenomes/plants")


def version():
    return int(os.getenv("ENSEMBLGENOMES_VERSION", config["ensemblgenomes"]["version"]))


if __name__ == "__main__":
    main()
