# Annual public review producer prerequisite

Before dispatching `.github/workflows/annual-only-public-review.yml`, the owner must provision an explicit `annual-public-review` environment with a main-only deployment branch policy and exactly one secret: `ANNUAL_PUBLIC_REVIEW_HMAC_KEY`. Later, the owner copies the existing HMAC root into that secret via stdin. This repository records no secret value. The environment is not provisioned by this change.
