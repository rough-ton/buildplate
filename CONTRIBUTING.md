# Contributing and releasing

End users never build Buildplate. They pull the published image from GitHub Container Registry. This page is for working on the code and cutting releases.

## Run from source

Requires Python 3.11 or newer, OpenSCAD and Xvfb.

```bash
sudo apt-get install -y openscad xvfb xauth
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
mkdir -p data
BUILDPLATE_DATA=./data BUILDPLATE_BUNDLED_LIBS=./data/libraries python -m app.main
```

Open `http://localhost:8080`. Static files in `app/static` are served directly, so a browser refresh picks up UI changes.

Run the tests:

```bash
pytest -q
```

To try the container locally before pushing:

```bash
docker build -t buildplate:dev .
docker run --rm -p 8080:8080 -v "$PWD/data:/data" buildplate:dev
```

## How images are published

`.github/workflows/build.yml` runs on GitHub-hosted runners:

| Event | Tests | Image |
| --- | --- | --- |
| Pull request | Yes | Not built |
| Push to `main` | Yes | Built, smoke-tested, then pushed as `edge` and `sha-<short>` |
| Tag `vX.Y.Z` | No (already tested on `main`) | Not rebuilt: the tested `sha-<short>` image is promoted to `X.Y.Z`, `X.Y` and `latest` |

On `main`, the image is built locally, started as an unprivileged user and checked (health, OpenSCAD, a render, a BOSL2 render) before anything is pushed, so a `sha-<short>` image always means the commit passed tests and the smoke test.

A release does not rebuild. It takes `sha-<short>` for the tagged commit and adds one tiny layer that only sets the version (`BUILDPLATE_VERSION` and the `org.opencontainers.image.version` label), checks that the image reports that version, and pushes it. Every application layer is byte-for-byte the image tested on `main`. If no tested image exists for the commit, the release fails and asks you to tag a commit that has passed on `main`.

`latest` only moves on a tagged release, so users on `latest` get release builds, not every commit. Use `edge` to test unreleased changes.

## Cutting a release

Tag a commit on `main` whose **Build and publish** run has finished successfully (it publishes the `sha-<short>` image the release promotes):

```bash
git switch main && git pull
git tag -a v0.2.0 -m "v0.2.0"
git push origin v0.2.0
```

Then create the GitHub release notes from the tag:

```bash
gh release create v0.2.0 --generate-notes
```

## One-time repository setup

1. Make the repository public so Actions minutes are free and unlimited.
2. After the first successful image push, make the package public so users can pull without logging in. GitHub only allows this in the web UI: **Profile > Packages > buildplate > Package settings > Change visibility > Public**. Confirm from any machine with:

   ```bash
   docker logout ghcr.io; docker pull ghcr.io/rough-ton/buildplate:edge
   ```
3. In the package settings, under **Manage Actions access**, confirm the repository has write access (it does by default when the workflow created the package).

## Updating the printer catalog

`app/data/printer_db.json` is generated from OrcaSlicer's printer profiles. The **Refresh printer catalog** workflow runs monthly (or on demand from the Actions tab) and opens a pull request when the list changes. To run it by hand:

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/SoftFever/OrcaSlicer /tmp/orca
git -C /tmp/orca sparse-checkout set resources/profiles
python3 tools/build_printer_db.py /tmp/orca
```

Machine styles for the illustrations are assigned in `tools/printer_types.py`: explicit rules per manufacturer and model first, then OrcaSlicer's `printer_structure` field, then name hints. Add a rule there when a new printer shows the generic illustration.

The workflow needs **Settings > Actions > General > Allow GitHub Actions to create and approve pull requests** turned on.
