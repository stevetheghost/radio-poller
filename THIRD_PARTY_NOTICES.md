# Third-party software

Radio Poller's own code is under the MIT license (see `LICENSE`). It includes or depends on the following,
which keep their own licenses.

## Included in this repository

These two files in `app/static/vendor/` are unmodified copies of the published packages. Each package's
license text is kept beside it.

| Component | Version | License | Copyright | License text |
|---|---|---|---|---|
| [daisyUI](https://daisyui.com) (`daisyui-5.7.50.css`) | 5.7.50 | MIT | Pouya Saadeghi | `app/static/vendor/daisyui-LICENSE.txt` |
| [Tailwind CSS](https://tailwindcss.com), browser build (`tailwindcss-browser-4.3.4.js`) | 4.3.4 | MIT | Tailwind Labs, Inc. | `app/static/vendor/tailwindcss-browser-LICENSE.txt` |

## Installed when the Docker image is built

| Component | License | Notes |
|---|---|---|
| [tzdata](https://pypi.org/project/tzdata/) (Python package) | Apache-2.0, Python Software Foundation | Timezone data, installed by `pip` from `requirements.txt`; not copied into this repository. It packages the IANA time zone database, which is in the public domain. |

The Docker image is also built on the official `python:3.12-alpine` image, which has its own licenses.

## Data from radio stations

Song titles, artist names, album art and station information come from iHeart's and Audacy's public
now-playing feeds and belong to their respective owners. This project is not affiliated with or endorsed by
iHeartMedia, Audacy, or any radio station.
