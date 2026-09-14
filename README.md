# Profilarr Database Template

Template for creating your own Profilarr-compliant database (PCD).

## Quick Start

1. Click **"Use this template"** → **"Create a new repository"**
2. Link the repository in Profilarr
3. Edit the manifest to customize your database
4. Start creating profiles and custom formats

## Structure

```
├── pcd.json      # Database manifest
├── ops/          # Your configuration operations
└── tweaks/       # Optional configuration variants
```

## Codec and size policy

All eight profiles score x265/H.265/HEVC at **3,000** and x264/H.264/AVC at
**1,000**. The codec bonus applies to encodes, excluding remuxes. Quality
ordering and Sonarr's season-pack preference still apply. Sonarr's x264
condition requires a codec title match so HEVC releases do not receive both
codec bonuses.

Minimum and preferred sizes for 720p, 1080p and 2160p HDTV, WEB and Blu-ray
encodes are about 30% lower. Examples in MB/min:

| Quality | Radarr minimum / preferred | Sonarr minimum / preferred |
| --- | --- | --- |
| WEB 1080p | 12 / 40 → 9 / 28 | 15 / 20 → 11 / 14 |
| Blu-ray 1080p | 51 / 60 → 36 / 42 | 15 / 25 → 11 / 18 |
| WEB 2160p | 34 / 50 → 24 / 35 | 40 / 45 → 28 / 32 |

These are starting targets, not a guarantee of equal image quality. The
[x265 documentation](https://x265.readthedocs.io/en/stable/cli.html#cmdoption-crf)
explains that the bitrate needed for a given quality depends on source
complexity. Sample grainy and high-motion releases before lowering these
targets further. Size definitions apply to both codecs. Maximum sizes stay
at their existing values to allow larger x264 fallbacks. Remux, disc, raw
and SD definitions retain their existing limits.

The policy lives in `CODEC_SCORES` and `HEVC_SIZES` in
[`scripts/regen-ops.py`](scripts/regen-ops.py). Size targets are absolute,
so exporting synced settings and regenerating cannot repeatedly shrink them.
Regenerate with `python3 scripts/regen-ops.py` after exporting your Arr settings,
then sync the profiles and `Local` quality definitions through Profilarr.
Automatic upgrades remain disabled; this does not transcode or automatically
replace existing downloads.

Run the regression check with `python3 scripts/test_hevc_policy.py`.

## Learn More

- [Profilarr Documentation](https://github.com/Dictionarry-Hub/profilarr)
- [Schema Reference](https://github.com/Dictionarry-Hub/schema)
- [Example Database](https://github.com/Dictionarry-Hub/db)
