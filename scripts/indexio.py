"""Write an index file and its gzip companion, only when the content changed."""
import gzip
from pathlib import Path


def write_index(out_file: Path, text: str) -> bool:
    """Write out_file and out_file.gz; report whether anything was written.

    Nothing is touched when out_file already holds text and the .gz exists, so
    the .gz only ever changes with the JSON. mtime=0 keeps the bytes of the .gz
    stable for identical text.
    """
    gz_file = out_file.with_name(out_file.name + ".gz")
    if gz_file.exists() and out_file.exists() and out_file.read_text(encoding="utf-8") == text:
        return False
    out_file.write_text(text, encoding="utf-8")
    with gzip.GzipFile(gz_file, "wb", mtime=0) as fh:
        fh.write(text.encode("utf-8"))
    return True
