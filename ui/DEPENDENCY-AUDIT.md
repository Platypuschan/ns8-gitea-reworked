# UI dependency audit (2026-11-10)

Baseline: `c52eba47a516041dece6de9306b4d304f75a2ee4`.

## Reproduction

Use the project-pinned Yarn 4.18.1 (the production builder uses Node 26.13.0):

```sh
yarn install --immutable
yarn explain peer-requirements
yarn lint
yarn build
yarn npm audit --all --recursive --json
```

The local audit used Node 24.22.2 and `npm exec --yes
--package=@yarnpkg/cli-dist@4.18.1 -- yarn ...` because Corepack is not
installed on the host. CI also exercises the pinned Node 26 builder.

## Compatible fixes

Keep the existing major versions and dependency ranges. Refresh vulnerable
transitive dependencies within those ranges with `yarn up -R ajv ansi-regex
baseline-browser-mapping browserslist cross-spawn js-yaml lodash semver
word-wrap`. Pin only the existing minimatch 3 descriptor and its
brace-expansion 1 dependency to patched, API-compatible releases, rather than
forcing minimatch 11's incompatible exports onto ESLint 6. No application,
Vue, Carbon or shared-library code is changed.

The recursive audit reports **40 findings before, 11 after** (including
package-deprecation notices):

| Severity | Before | After |
| --- | ---: | ---: |
| high | 23 | 3 |
| moderate | 14 | 6 |
| low | 3 | 2 |

The minimatch findings actually returned for this lockfile are
GHSA-f8q6-p94x-37v3, GHSA-3ppc-4f35-3m26, GHSA-7r86-cg39-jmmj and
GHSA-23c5-xmqv-rm74; all disappear after the update. The audit did not return
GHSA-g3hv-75wg-m249, so this is not a claim to have reproduced that advisory.

## Retained dependencies and warnings

Usage was checked in both `src/` and installed
`node_modules/@nethserver/ns8-ui-lib` 2.2.1, not just application imports:

- **lottie-web-vue**: `NsEmptyState` uses `NsLottieAnimation`, which imports
  `LottieAnimation`. Keep it and the existing registration. The lottie-web
  `eval` build warning remains; removing it would affect shared components.
- **vue-axios**: the shared `TaskService` calls `this.axios.get/post`. Keep
  the plugin that supplies this API; a direct application import is not a
  compatible replacement.
- **vue-debounce**: `NsDataTable` and `NsComboSearchBox` use `v-debounce`.
  Keep the global directive registration.
- **ESLint peer conflict**: Babel's parser requires ESLint >=7.5, whereas
  eslint-plugin-vue 6 requires ESLint 5/6. These ranges do not overlap.
  An ESLint/plugin migration is separate work; do not falsify peer metadata.
- **rollup peer**: ns8-ui-lib ships build-time @rollup/plugin-json and
  rollup-plugin-terser without declaring their Rollup peer. Those are the
  library's old build tools, not this application's Vite pipeline. Do not
  install a second legacy Rollup merely to hide the metadata warning.
  No VueUse peer warning was reported in this dependency tree.
- **Host CSS**: `/cluster-admin/css/core.css` is supplied by NS8 at runtime;
  Vite correctly leaves the URL unresolved at build time.
- No CJS/ESM build warning was observed. Before and after builds produced the
  same asset names, hashes and sizes (681 modules; main JS 1,582.51 kB).

## Remaining audit findings

These require dependency-owner/major changes beyond compatible range refreshes:

| Dependency | Findings / scope |
| --- | --- |
| flatted 2.0.2 | GHSA-25h7-pfq9-p65f, GHSA-rf6f-7fwh-wjgh (high); ESLint's flat-cache 2 requires flatted 2, patched versions are 3.x |
| tmp 0.0.33 | GHSA-52f5-9888-hmc6 (low), GHSA-ph9p-34f9-6g65 (high); ESLint -> inquirer -> external-editor requires the old major |
| Vue 2.7.16 | GHSA-5j4c-8p2g-v4jx (low), compiler parseHTML ReDoS; no compatible Vue 2 fix returned |
| carbon-components, eslint, glob, inflight, rimraf, vue-i18n | Six moderate deprecation/support notices |

This is a reduction, **not a vulnerability-free audit**. Do not force flatted
3 or tmp 0.2 across declared major/API boundaries, remove needed plugins,
suppress audit advisories, or migrate the Vue 2/Carbon 10 host contract as part
of this cleanup. The three remaining high findings are in development tooling,
not proof of a remotely exploitable deployed UI. The Vue finding remains a
runtime-stack maintenance risk and needs a separately tested migration.
