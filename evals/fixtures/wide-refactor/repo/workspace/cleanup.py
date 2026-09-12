import shutil


def remove(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
