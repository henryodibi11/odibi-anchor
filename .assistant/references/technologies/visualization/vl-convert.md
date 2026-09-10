# vl-convert Engineering Reference

**Researched:** 2026-08-14 | **Baseline:** current 1.x package; query its version map at runtime  
**Evidence:** [D] package docs; [S] public source; [U] fonts and target runtime

`vl-convert` packages Vega/Vega-Lite compilation and rendering behind native libraries. The Python
package can convert Vega-Lite to Vega and render Vega/Vega-Lite as SVG, PNG, PDF, HTML, or scenegraph
outputs without a separately managed browser driver. Exact function names and supported options must
be checked against the installed package. [D]

## Typical boundary

```python
import vl_convert as vlc

vega_spec = vlc.vegalite_to_vega(vl_spec)
svg_text = vlc.vegalite_to_svg(vl_spec)
png_bytes = vlc.vegalite_to_png(vl_spec, scale=2)
```

Pass a Python dict or JSON according to the installed signature. Pin `vl-convert-python`, record its
reported Vega/Vega-Lite version mapping, and retain output media type, scale, locale, and warnings.
Compilation output is useful for diagnosing Altair/Vega-Lite behavior but should not become the
human-maintained source artifact.

## Deterministic export

- Validate input before conversion and fail on conversion errors.
- Pin package and target Vega-Lite version where the API permits.
- Fix dimensions, scale, locale/timezone, and data ordering.
- Provision required fonts in the export environment; font fallback changes layout.
- Hash the canonical input spec and output bytes for retained evidence.
- Avoid asserting byte-identical PNG/PDF across different operating systems without qualification.

SVG is inspectable and vector-friendly but can be large for many marks. PNG is predictable for social
media and documents but rasterized. PDF is useful for print but font embedding needs testing. HTML can
retain interaction but embeds scripts and may load resources; treat it as active content.

## Security and reliability

Vega specs can reference remote data/images and contain expressions. Apply a trust policy before
conversion. Disable or reject remote URLs for untrusted specs, bound input and output sizes, limit
execution time in a worker boundary, and do not expose unrestricted conversion as a public service.
Conversion success does not prove accessibility or Deneb compatibility.

## Version mapping

The package bundles JavaScript grammar/runtime versions independently of Altair and Deneb. Never infer
the compiler version from the Python package number. Inspect the package's supported-version API or
release metadata and compare it with `$schema` and destination host baselines. A single engine may need
separate export profiles for browser preview and Deneb.

## Failure modes

- Missing native wheel for a platform/architecture.
- Unsupported requested Vega-Lite version.
- Remote resources unavailable in an offline runtime.
- Fonts absent, causing wrapping or clipping drift.
- Invalid datetime/NaN values or huge inline datasets.
- Assuming static export preserved hover, selection, or keyboard behavior.
- Treating successful compilation as visual correctness.

## Sources and refresh

- https://github.com/vega/vl-convert
- https://pypi.org/project/vl-convert-python/

Refresh when conversion signatures, bundled versions, wheel platforms, or font/resource behavior
changes. Add [V] only for exact package, platform, format, and retained checks.
