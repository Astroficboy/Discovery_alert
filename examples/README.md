# Example editions

Two complete sample editions, rendered by the same
[`Renderer`](../src/delivery/renderer.py) and the same
[template](../templates/newsletter.html) that a real edition uses, so they
cannot drift away from what the system actually produces.

| | Edition | Domains | Words |
|---|---|---|---|
| [`issue-001.html`](issue-001.html) | The woman nobody asked to name | photography · history · people · culture | 812 |
| [`issue-002.html`](issue-002.html) | The instrument you play by not touching it | music · technology · science · people | 768 |

Open the `.html` files in a browser. The `.txt` files are the plain-text
alternative part that goes in the same email. The `.json` files are the source
of truth — the `.html` and `.txt` are generated from them:

```bash
python scripts/build_examples.py           # render
python scripts/build_examples.py --check   # validate without writing
```

The build script puts each example through the project's own licence gate and
quality thresholds and **fails if an example would not have been sent**. It
runs in CI for that reason: an example that could not clear the project's own
bar would be a poor advertisement for it.

## What these are, and are not

They are **hand-written demonstrations of the target quality**, not transcripts
of a pipeline run. They exist so you can see what "good" means here before
spending a token, and so the template has something realistic to be tested
against.

The facts in them are drawn from the same bundled research dossiers the offline
mode uses ([`src/fixtures/candidates.json`](../src/fixtures/candidates.json)),
and each carries the same claim-confidence discipline a generated edition
would: established facts are stated, contested ones are marked as contested.
Issue 002's account of how Leon Theremin came to leave New York, for instance,
is written as disputed, because it is.

## Image references

Both editions reference Wikimedia Commons files through
`Special:FilePath`, which resolves a file by **name** rather than by content
hash. That keeps the reference valid as long as the file exists under that
name — but files do get renamed and occasionally deleted. To check:

```bash
python -m src.main verify-links
```

That checks every bundled fixture image and every example image, and reports
which references have rotted. If one has, replace the `image.url` and
`image.page_url` in the corresponding `.json` and re-run the build script.
