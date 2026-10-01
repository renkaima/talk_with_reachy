"""Create the private Hugging Face Space for Talk with Reachy and upload this repository to it.

Called by publish_to_hf.sh after you have signed in with `hf auth login`.
Usage: python publish_space.py <repo-dir> <space-name>
"""

import sys
from pathlib import Path

from huggingface_hub import HfApi


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
]


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
    print(f"Done: https://huggingface.co/spaces/{repo_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
