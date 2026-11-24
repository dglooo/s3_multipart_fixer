from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parent.parent))

from s3_uploader.upload_interface import main


if __name__ == "__main__":
    main()
