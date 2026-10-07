# fineract-customization — build notes

## Pinned versions

- Fineract release tag: `1.10.0` (confirmed to exist upstream: https://github.com/apache/fineract/releases/tag/1.10.0)
- JDK: 17 (confirmed from the tag's `build.gradle`: `sourceCompatibility = JavaVersion.VERSION_17` / `targetCompatibility = JavaVersion.VERSION_17`)
- Build tool: Gradle 8.6, via the `gradlew` wrapper bundled in the source tree (confirmed from `gradle/wrapper/gradle-wrapper.properties` at that tag)
- Runtime base image: `azul/zulu-openjdk-alpine:17` — the same base Fineract's own Gradle Jib plugin config uses (`fineract-provider/build.gradle` → `jib { from { image = 'azul/zulu-openjdk-alpine:17' } ... }`), so our hand-built image behaves the same as the one the project itself would produce.
- Main class: `org.apache.fineract.ServerApplication` (also confirmed from the same `jib { container { mainClass = ... } }` block, and independently from Fineract's own docs).
- Ports: 8080 (HTTP) and 8443 (HTTPS, the one actually used) — same as upstream's Jib config.
- JDBC driver: `org.postgresql:postgresql` is already an `implementation` dependency of `fineract-provider` (confirmed in its `build.gradle`), so it's bundled into the bootJar fat jar automatically — no separate driver wiring needed for Postgres.

There is no version-pinned official image on Docker Hub (`apache/fineract` is only published as `latest`, `develop`, and raw commit SHAs — no `1.10.0` tag), so "the unmodified baseline" is built from source too, using this same Dockerfile with an empty `fineract.patch` (see below), rather than `docker pull`.

## Why a hand-written multi-stage Dockerfile, not Jib

Upstream's own CI (`.github/workflows/build-docker-postgresql.yml`) builds its image with:

```
./gradlew --no-daemon --console=plain :fineract-provider:clean :fineract-provider:build :fineract-provider:jibDockerBuild -x test -x cucumber
```

`jibDockerBuild` needs a running Docker daemon *during the Gradle build itself* to load the image it builds. That works fine on a CI runner with the host's Docker daemon available, but not from inside a plain `docker build` (no daemon-in-daemon). So instead we do a conventional two-stage build: compile with Gradle in a builder stage, then copy just the resulting `fineract-provider.jar` into a slim runtime stage on the same base image. Functionally equivalent image, buildable with plain `docker build` / `docker compose build`.

## Why tests are skipped in the image build (`-x test -x cucumber`)

Same reasoning upstream uses in the command above: running Fineract's full existing regression + cucumber suite on every image build is impractical (large suite, long runtime) and isn't what the assessment is actually asking for evidence of. The assessment requires evidence that **our new behavior** (Part A) is covered by JUnit tests and that they pass — that's captured separately by `scripts/run-fineract-tests.sh`, which runs only the new test classes we add and saves the log to `evidence/fineract-java-tests.log`. This keeps the Docker build fast and reproducible while still producing the required test-pass evidence.

## Generating `fineract.patch`

Right now it's an empty file on purpose — `git apply` on an empty patch is a no-op, so the Dockerfile currently builds the *unmodified* pinned tag. This is the Phase 0 baseline, used to prove the build/run mechanism works before any customization lands.

Once Part A's changes are written, regenerate it:

```sh
git clone --branch 1.10.0 https://github.com/apache/fineract.git /tmp/fineract-upstream
cd /tmp/fineract-upstream
# ... apply the Liquibase changelog + Java command-handler changes here ...
git diff > fineract.patch
cp fineract.patch <repo>/fineract-customization/fineract.patch
```

(`/tmp/fineract-upstream` here is a throwaway local scratch clone, not something committed to this repo — see the repo-layout discussion in DECISIONS.md for why we don't vendor the upstream source.)

## Issues hit while validating the build (and fixes)

Three real problems surfaced getting the baseline (empty-patch) build to run end to end — none hypothetical, all reproduced and fixed:

1. **`git apply` on an empty patch is not a no-op.** It errors with `No valid patches in input`. Fixed with `--allow-empty`.
2. **Dead transitive buildscript dependency.** The root `build.gradle`'s Asciidoctor-docs plugin chain (`org.asciidoctor.jvm.gems` → `jruby-gradle-core-plugin` → `http-builder-ng-okhttp`) pulls in `com.burgstaller:okhttp-digest:1.10`, whose group was retired when JCenter shut down in 2022 (moved to `io.github.rburgst:okhttp-digest`). This breaks even a completely stock, unmodified build of tag `1.10.0` today — confirmed by reproducing it before touching anything patch-related. Fixed via `gradle-init.gradle`, a dependency-substitution init script applied at build time (not a change to Fineract's own files).
3. **Fineract's own `gradle.properties` requests `-Xmx6g` plus `org.gradle.parallel=true`.** Fine for CI, but this build may run on memory-constrained dev hardware (confirmed: a machine with ~8GB total RAM, where the default WSL2 ceiling alone is under 4GB) where that specification exceeds what's available to the whole build environment and gets the build killed mid-compile (observed as a BuildKit `rpc error: ... EOF`, not a Gradle error). Fixed with two `sed` edits after cloning: heap down to `-Xmx2g`, parallel execution off. Every other JVM flag (the `--add-opens`/`--add-exports` needed for the compiler's internal APIs) is left untouched.
4. **`bootJar`'s output filename isn't `fineract-provider.jar`.** The task sets `archiveClassifier = ''` but doesn't override `archiveFileName`, so Gradle's default naming applies: `fineract-provider-<project.version>.jar`. The final `COPY` in the runtime stage globs (`fineract-provider*.jar`) instead of hardcoding a version string that's computed by the build, not fixed in source.

Validated end to end: `docker compose up` brings up Postgres (healthy, both tenant DBs created) and the custom Fineract image (healthy), with `https://localhost:8443/fineract-provider/actuator/health` returning `{"status":"UP"}` and `.../actuator/info` confirming the exact pinned commit or Fineract (`tags: "1.10.0"`, matching commit hash from the clone step).

## Known simplifications (to expand in DECISIONS.md)

- Runtime container runs as the image's default user, not Jib's `nobody:nogroup` — fine for a local/dev assessment stack, called out here as a deliberate simplification rather than an oversight.
- Build-time JVM/Gradle flags for code-quality gates (`-x spotlessCheck -x checkstyleMain`) are skipped for build speed; not a correctness concern since they don't affect runtime behavior.
