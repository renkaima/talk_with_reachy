"""Create the private Hugging Face Space for Talk with Reachy and upload this repository to it.

Called by publish_to_hf.sh after you have signed in with `hf auth login`.
Usage: python publish_space.py <repo-dir> <space-name>
"""

import sys
from pathlib import Path

from huggingface_hub import HfApi


SECRET_FILE = "deploy/google_client_secret.txt"  # git-ignored; holds only the Google client secret
MODULE_FILE = "src/talk_with_reachy/google_drive_upload.py"
SECRET_LINE = 'CLIENT_SECRET = ""'
SPACE_HEADER_FILE = "deploy/space_header.yml"  # the Space settings; see upload_readme
APP_TAG = "reachy_mini_python_app"  # the Control App lists Spaces with this tag

# Files that are local tooling or caches, not part of the app.
IGNORE = [
    ".gitattributes",
    ".venv/*",
    "deploy/.deploy-venv/*",
    "deploy/.onedrive-venv/*",
    "deploy/.google-venv/*",
    "*/__pycache__/*",
    "__pycache__/*",
    "*.pyc",
    ".mypy_cache/*",
    ".ruff_cache/*",
    ".pytest_cache/*",
    "build/*",
    "dist/*",
    "*.egg-info/*",
    "*/*.egg-info/*",
    ".DS_Store",
    "*/.DS_Store",
    SECRET_FILE,
    "README.md",  # uploaded by upload_readme, with the Space settings on top
]


def upload_client_secret(api: HfApi, repo_id: str, repo_dir: Path) -> None:
    """Upload google_drive_upload.py again with the Google client secret filled in.

    The secret is not in the public git repository, so the private Space is the
    only place robots get it from.
    """
    secret_path = repo_dir / SECRET_FILE
    if not secret_path.is_file() or not secret_path.read_text(encoding="utf-8").strip():
        print(f"Note: {SECRET_FILE} is missing, so robots that install this version cannot sign in to Google Drive.")
        return
    secret = secret_path.read_text(encoding="utf-8").strip()
    source = (repo_dir / MODULE_FILE).read_text(encoding="utf-8")
    if source.count(SECRET_LINE) != 1:
        print(f"Warning: could not find the line {SECRET_LINE} in {MODULE_FILE}, so the secret was not added.")
        return
    api.upload_file(
        path_or_fileobj=source.replace(SECRET_LINE, f'CLIENT_SECRET = "{secret}"').encode("utf-8"),
        path_in_repo=MODULE_FILE,
        repo_id=repo_id,
        repo_type="space",
        commit_message="Add the Google client secret",
    )
    print("Added the Google client secret to the Space copy.")


def upload_readme(api: HfApi, repo_id: str, repo_dir: Path) -> None:
    """Upload README.md with the Space settings from deploy/space_header.yml on top.

    Hugging Face reads a Space's settings (title, tags, ...) from a YAML header at the top of
    README.md. GitHub would show that header as a table, so the repository keeps it in a
    separate file and only the Space copy gets it.
    """
    header = "\n".join(
        line
        for line in (repo_dir / SPACE_HEADER_FILE).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )
    readme = (repo_dir / "README.md").read_text(encoding="utf-8")
    api.upload_file(
        path_or_fileobj=f"---\n{header}\n---\n\n{readme}".encode("utf-8"),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="space",
        commit_message="Add README.md with the Space settings",
    )


def main() -> int:
    """Create the Space if needed, refuse to publish into a public one, then upload."""
    repo_dir, space_name = Path(sys.argv[1]).resolve(), sys.argv[2]
    api = HfApi()
    user = api.whoami()["name"]
    repo_id = f"{user}/{space_name}"

    api.create_repo(repo_id, repo_type="space", space_sdk="static", private=True, exist_ok=True)
    if not api.space_info(repo_id).private:
        print(f"Stopped: https://huggingface.co/spaces/{repo_id} already exists and is public.")
        print("Make it private in the Space settings, or run again with SPACE_NAME=<another name>.")
        return 1

    print(f"Uploading to private Space {repo_id} ...")
    api.upload_folder(
        repo_id=repo_id,
        repo_type="space",
        folder_path=repo_dir,
        ignore_patterns=IGNORE,
        commit_message="Deploy Talk with Reachy",
    )
    upload_readme(api, repo_id, repo_dir)
    upload_client_secret(api, repo_id, repo_dir)
    if APP_TAG not in (api.space_info(repo_id).tags or []):
        print(f"Warning: the Space has no {APP_TAG} tag, so the Control App will not list it. Tell Claude.")
        return 1
    print(f"Checked: the Space has the {APP_TAG} tag, so the Control App lists it.")
    print(f"Done: https://huggingface.co/spaces/{repo_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
