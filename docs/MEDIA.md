# Media

The image, video and speech pipeline. The product asks for a **capability** and
some provider does the work: the MiniMax connector, an OpenAI-compatible server,
ComfyUI on this Mac, Ollama, a cloud image API, or the macOS speech
synthesiser.

Everything is reachable from the **Media** page (`/media`) and from
`/api/media/*`. Without that page the engine would be API-only.

## The product asks for a capability, not for a vendor

This is the rule the whole module is built around. The product never says
"call MiniMax"; it says "I need an image", and the framework decides who
answers. Everything else follows from it:

* a provider declares the capabilities it can serve and the rules of each
  one (which models, which durations, which keyframe modes);
* the configuration — base url, key, model names — lives in the database
  (`media_providers`), never in the code;
* **local providers are preferred over remote ones** when they are set up,
  because work done on this machine is free, fast and private;
* adding a vendor is one class and one `register()` call — not a fork.

The seam is a single function:

```python
provider = media.pick_provider("image")          # whichever one is set up
provider = media.pick_provider("image", "comfyui")  # or exactly this one
```

Every generating endpoint also accepts `"provider": "<key>"`, so the page can
offer the choice without the product knowing a vendor.

## What it can do

| Capability | Call | Notes |
|---|---|---|
| text-to-image | `POST /api/media/image` | 1K / 2K / 4K, six aspect ratios |
| image-to-image | `POST /api/media/vary` | variations of a library asset |
| image-to-video | `POST /api/media/animate` | one call: asset id in, job out |
| text-to-video | `POST /api/media/video` | asynchronous, returns a `job_id` |
| assembly | `POST /api/media/assemble` | ffmpeg: cut, mix a music bed, burn subtitles |
| speech | `POST /api/media/speech` | text-to-speech |

Everything generated is stored under `<data dir>/media/<project>/` and recorded
in `media_assets`, so the library is a record, not a folder full of loose
files.

## The providers

| Key | Does | Where it runs |
|---|---|---|
| `matrix` | image, video, speech | the MiniMax desktop runtime, through `mcode-tools` |
| `openai` | text, embeddings, image, speech | anything speaking the OpenAI API: OpenAI, DeepSeek, Together, Groq, LM Studio, llama.cpp, vLLM, Ollama's `/v1` |
| `ollama` | text, embeddings | a model server on this Mac |
| `comfyui` | image, video | ComfyUI on this Mac, driven by a workflow JSON |
| `macos_say` | speech | the macOS speech synthesiser |
| `piper` | speech | a local piper server or the binary |
| `stability` | image | Stability AI: Core, SD3, Ultra |
| `replicate` | image | any published Replicate model, as a prediction |
| `fal` | image | fal.ai endpoints |
| `local_onnx` | embeddings | MiniLM with ONNX Runtime: no torch, no network |

`GET /api/media/providers` returns the whole catalogue with its state.
`POST /api/media/providers/<key>` configures one; the API key is write-only —
it goes in and never comes back out, and an empty field keeps the stored one.

### The local embedding model

`sentence-transformers` needs torch, which is far too heavy to ship inside the
desktop app. ONNX Runtime runs the *same* model (all-MiniLM-L6-v2, 384
dimensions) in a fraction of that space, and the vectors are the same ones
torch produces — so an index built in development is readable by the packaged
app. The model tag is the same on both sides.

The model is downloaded on demand into `<data dir>/models/`, and it is **not**
bundled: `fastembed` and `onnxruntime` have to be installed for the provider
to work. When they are missing, the provider says so and the search falls back
to FTS5, which is already there and needs nothing.

For Spanish, `jinaai/jina-embeddings-v2-base-es` (768 dimensions) is the one
to choose: it puts "un gato duerme en el sofá" and "el gato está durmiendo en
el sofá" at 0.97, and keeps "el precio del oro" near the trading documents at
0.45. It costs 612 MB of disk against 86 MB for the English model.

## What the work costs

Every call is counted against its provider (`media_provider_usage`), and every
provider can carry a **daily cap**: the number of calls it may make before it
refuses. The cap is the brake — a render that costs money should stop when the
user says so, not when the vendor's limit says so — and the counter is how the
user sees it coming. The page shows today's calls next to each provider.

## Video is asynchronous, on purpose

`MiniMax-H3` takes minutes to render and returns a task id, not a file.
ConductLoop therefore keeps its own `media_jobs` table:

1. `POST /api/media/video` submits and returns `job_id` immediately;
2. `GET /api/media/video/<job_id>` polls and, on success, downloads the file
   and records it as an asset;
3. the scheduler calls `reap_jobs()` on every tick, so the flow completes even
   if the page was closed.

A job is given up on after `MAX_JOB_ATTEMPTS` polls or `MAX_JOB_AGE_SECONDS`.
Without that, a task the engine has forgotten would be re-polled on every tick
for the life of the install.

A job remembers **which provider** accepted it, and it is that provider which
is asked about it later.

## The models are not interchangeable

The rules below come from the live `submit_video_generation` schema and were
checked against real submissions. Getting one wrong costs money.

| Model | Durations | Resolutions | Ratio | Keyframe | Sound |
|---|---|---|---|---|---|
| `MiniMax-H3` | 4–15 s | 768P, 2K | yes | first, last | yes |
| `MiniMax-H3-Max` | 5–15 s | 480P, 768P | yes | first, last | yes |
| `MiniMax-Hailuo-2.3` | 6 s, 10 s | 768P (1080P only at 6 s) | **no** | first, subject | **no** |

* In **keyframe mode** the aspect follows the source image (`adaptive`), so a
  ratio is meaningless there.
* `Hailuo-2.3` rejects `ratio` outright.
* The H3 family renders its own **sound track**; Hailuo is silent.

That table belongs to the **vendor**, so it lives with the vendor:
`MatrixVideoContract.video_options()` is what the product asks. A provider
that does not publish a table gets the benefit of the doubt — a wrong
rejection costs the user a render they could have paid for.

The page builds its form from `/api/media/status`, so it can only offer
combinations the provider accepts.

## Falling back when the wallet says no

The H3 family is billed against the M Plan weekly limit or account credits.
When the account cannot cover it the engine answers `402`, and a legacy Token
Plan cannot pay for H3 at all — but it can pay for Hailuo-2.3.

So a refused render is retried once on `MiniMax-Hailuo-2.3`, and the job
records `requested_model`, `fell_back` and the model's real capabilities.
Without that, a billing state killed the user's render even though the cheaper
model would have run.

## Image-to-video needs the file on the engine's side

The connector takes **urls only** — no paths, no base64, no data URIs. So:

```
local keyframe → mcode-tools upload-temp-url → temp_url → input_image
```

`MatrixEngine.upload()` does that, refusing anything over 30 MB or in a format
the engine does not accept.

## Prompt presets

A bare sentence is a weak instruction. `IMAGE_PRESETS` holds six shots that
actually come up when running a product — app icon, landing hero, video
thumbnail, product photo, avatar, abstract background. Each is a complete
prompt with a `{subject}` slot, so the result is something you can keep
editing rather than a black box.

`GET /api/media/presets`, or `POST /api/media/image` with
`{"preset": "app_icon", "subject": "..."}`.

## What leaves this Mac

Media spends credits, so:

* every generating endpoint refuses a request whose `Host` is not loopback;
* a keyframe may only be a file under `<data dir>/media/` — a path anywhere
  else is an error, never a silent downgrade to text-to-video;
* `/api/media/file/<id>` serves a library asset by id, so the client never
  handles a filesystem path.

## Layout

```
ops_dashboard/media.py           product layer: capabilities, presets, ffmpeg
ops_dashboard/media_providers.py the framework: capabilities, registry, budget
ops_dashboard/media_backends.py  the providers themselves
ops_dashboard/db_media.py        media_assets + media_jobs + provider usage
ops_dashboard/routes_media.py    /media page and /api/media/*
ops_dashboard/web/media.html     the workbench
tests/test_media*.py             engine, providers, routes, page, reaper
```

## Video is asynchronous, on purpose

`MiniMax-H3` takes minutes to render and returns a task id, not a file.
ConductLoop therefore keeps its own `media_jobs` table:

1. `POST /api/media/video` submits and returns `job_id` immediately;
2. `GET /api/media/video/<job_id>` polls and, on success, downloads the file
   and records it as an asset;
3. the scheduler calls `reap_jobs()` on every tick, so the flow completes even
   if the page was closed.

A job is given up on after `MAX_JOB_ATTEMPTS` polls or `MAX_JOB_AGE_SECONDS`.
Without that, a task the engine has forgotten would be re-polled on every tick
for the life of the install.

## The models are not interchangeable

The rules below come from the live `submit_video_generation` schema and were
checked against real submissions. Getting one wrong costs money.

| Model | Durations | Resolutions | Ratio | Keyframe | Sound |
|---|---|---|---|---|---|
| `MiniMax-H3` | 4–15 s | 768P, 2K | yes | first, last | yes |
| `MiniMax-H3-Max` | 5–15 s | 480P, 768P | yes | first, last | yes |
| `MiniMax-Hailuo-2.3` | 6 s, 10 s | 768P (1080P only at 6 s) | **no** | first, subject | **no** |

* In **keyframe mode** the aspect follows the source image (`adaptive`), so a
  ratio is meaningless there.
* `Hailuo-2.3` rejects `ratio` outright.
* The H3 family renders its own **sound track**; Hailuo is silent.

`VideoSpec` in `media.py` holds this table. The page builds its form from
`/api/media/status`, so it can only offer combinations the engine accepts.

## Falling back when the wallet says no

The H3 family is billed against the M Plan weekly limit or account credits.
When the account cannot cover it the engine answers `402`, and a legacy Token
Plan cannot pay for H3 at all — but it can pay for Hailuo-2.3.

So a refused render is retried once on `MiniMax-Hailuo-2.3`, and the job
records `requested_model`, `fell_back` and the model's real capabilities.
Without that, a billing state killed the user's render even though the cheaper
model would have run.

## Image-to-video needs the file on the engine's side

The connector takes **urls only** — no paths, no base64, no data URIs. So:

```
local keyframe → mcode-tools upload-temp-url → temp_url → input_image
```

`MatrixEngine.upload()` does that, refusing anything over 30 MB or in a format
the engine does not accept.

## Prompt presets

A bare sentence is a weak instruction. `IMAGE_PRESETS` holds six shots that
actually come up when running a product — app icon, landing hero, video
thumbnail, product photo, avatar, abstract background. Each is a complete
prompt with a `{subject}` slot, so the result is something you can keep
editing rather than a black box.

`GET /api/media/presets`, or `POST /api/media/image` with
`{"preset": "app_icon", "subject": "..."}`.

## What leaves this Mac

Media spends credits, so:

* every generating endpoint refuses a request whose `Host` is not loopback;
* a keyframe may only be a file under `<data dir>/media/` — a path anywhere
  else is an error, never a silent downgrade to text-to-video;
* `/api/media/file/<id>` serves a library asset by id, so the client never
  handles a filesystem path.

## Layout

```
ops_dashboard/media.py         engine, validation, presets, ffmpeg, product ops
ops_dashboard/db_media.py      media_assets + media_jobs
ops_dashboard/routes_media.py  /media page and /api/media/*
ops_dashboard/web/media.html   the workbench
tests/test_media*.py           engine, routes, page, reaper
```