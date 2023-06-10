"""http://plantregmap.gao-lab.org/."""
import logging
import re

from aligons import db
from aligons.extern import htslib
from aligons.util import cli, fs

from . import tools

_log = logging.getLogger(__name__)
HOST = "plantregmap.gao-lab.org"


def main(argv: list[str] | None = None):
    parser = cli.ArgumentParser()
    parser.add_argument("-D", "--download", action="store_true")
    parser.add_argument("species", nargs="?", default=".")
    args = parser.parse_args(argv or None)
    if args.download:
        cli.wait_raise(retrieve_deploy(q) for q in iter_download_queries())
    for x in fs.sorted_naturally(rglob("*.*", args.species)):
        print(x)


def rglob(pattern: str, species: str = "."):
    for species_dir in local_db_root().iterdir():
        if re.search(species, species_dir.name, re.IGNORECASE):
            for x in species_dir.rglob(pattern):
                yield x


def retrieve_deploy(query: str):
    url = f"http://{HOST}/download_ftp.php?{query}"
    outfile = local_db_root() / query.split("/", 1)[1]
    if outfile.suffix in (".bed", ".gff", ".txt"):
        outfile = outfile.parent / (outfile.name + ".gz")
    elif outfile.name.endswith(".gtf.gz"):
        outfile = outfile.with_suffix("").with_suffix(".gff.gz")
    future = tools.retrieve_compress(url, outfile)
    return cli.thread_submit(htslib.try_index, future)


def iter_download_queries():
    for query in iter_download_queries_all():
        if re.search(r"Oryza_sativa_Japonica|Solanum_lycopersicum", query):
            yield query


def iter_download_queries_all():
    content = download_php()
    for mobj in re.finditer(r"download_ftp\.php\?([^\"']+)", content):
        yield mobj[1]


def download_php():
    url = f"http://{HOST}/download.php"
    cache = local_db_root() / "download.php.html"
    return tools.retrieve_cache(url, cache).decode()


def local_db_root():
    return db.path("plantregmap")


if __name__ == "__main__":
    main()
