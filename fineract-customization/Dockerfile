# fineract-customization/Dockerfile
#
# Builds Apache Fineract from source at a pinned release tag, with our
# fineract.patch applied, and bakes the result into a runtime image.
#
# Pinned: tag 1.10.0, JDK 17, Gradle 8.6 (gradlew wrapper, bundled with the
# source tree itself -- confirmed from the tag's own
# gradle/wrapper/gradle-wrapper.properties and build.gradle
# sourceCompatibility/targetCompatibility).
#
# Runtime base, main class, ports and JVM flags mirror what Fineract's own
# Gradle Jib config (fineract-provider/build.gradle -> `jib { ... }`) bakes
# into the official image, so our custom image behaves the same way:
#   from.image   = azul/zulu-openjdk-alpine:17
#   mainClass    = org.apache.fineract.ServerApplication
#   ports        = 8080/tcp, 8443/tcp
#   args         = -Duser.home=/tmp -Dfile.encoding=UTF-8 -Duser.timezone=UTC
#                  -Djava.security.egd=file:/dev/./urandom
#
# We do NOT use Jib's `jibDockerBuild` task to produce this image: that task
# needs access to a running Docker daemon *during the Gradle build itself*
# (daemon-in-daemon), which doesn't work from inside a plain `docker build`.
# A conventional multi-stage build (compile with Gradle, then copy the jar
# into a slim runtime stage) gets the same result without that dependency.
#
# Tests are intentionally skipped in THIS build stage (-x test -x cucumber),
# matching Apache Fineract's own CI for building deployable images
# (.github/workflows/build-docker-postgresql.yml does the same: running the
# full upstream regression + cucumber suite on every image build is
# impractical -- it's a separate CI job upstream, and a separate evidence
# step for us: see scripts/run-fineract-tests.sh, which runs just our new
# JUnit tests for Part A and saves the log to evidence/).

ARG FINERACT_TAG=1.10.0

# ---- Stage 1: fetch pinned source, apply patch, compile ----
FROM azul/zulu-openjdk-alpine:17 AS builder
ARG FINERACT_TAG

RUN apk add --no-cache git bash

WORKDIR /workspace

# 1. Dynamically fetch the pinned, official release tag (internet access
#    during the build is explicitly permitted by the assessment brief).
RUN git clone --depth 1 --branch "${FINERACT_TAG}" https://github.com/apache/fineract.git fineract-src

WORKDIR /workspace/fineract-src

# 2. Apply our patch (Liquibase migration + Java changes for Part A) against
#    that exact tag.
COPY fineract.patch /workspace/fineract.patch
RUN git apply --verbose --allow-empty /workspace/fineract.patch

# 2b. Work around a dead upstream transitive buildscript dependency (JCenter
#     shutdown fallout, unrelated to our patch) -- see gradle-init.gradle.
COPY gradle-init.gradle /workspace/gradle-init.gradle

# 2c. Fineract's own gradle.properties asks for a 6 GB JVM heap plus parallel
#     task execution (org.gradle.jvmargs=-Xmx6g, org.gradle.parallel=true) --
#     fine on a CI runner, but this build may run on memory-constrained dev
#     hardware where that alone exceeds what's available to the whole build
#     environment. Dial both down (keep every other flag, e.g. the
#     --add-opens/--add-exports needed for compiler-internals access)
#     rather than touching anything patch-related.
RUN sed -i 's/-Xmx6g/-Xmx2g/' gradle.properties \
 && sed -i 's/org.gradle.parallel=true/org.gradle.parallel=false/' gradle.properties

# 3. Compile the customized Fineract provider into an executable fat jar.
#    (Gradle needs network access here too, to pull its dependency graph --
#    also covered by "internet access is permitted".)
RUN ./gradlew --no-daemon --console=plain \
      --init-script /workspace/gradle-init.gradle \
      :fineract-provider:clean \
      :fineract-provider:bootJar \
      -x test -x cucumber -x spotlessCheck -x checkstyleMain

# ---- Stage 2: slim runtime image ----
FROM azul/zulu-openjdk-alpine:17 AS runtime

WORKDIR /app

# bootJar sets archiveClassifier='' but doesn't override archiveFileName, so
# Gradle's default naming applies: fineract-provider-<project.version>.jar.
# Glob instead of hardcoding that version string, since it's computed by the
# build (e.g. from git describe) rather than fixed in source.
COPY --from=builder /workspace/fineract-src/fineract-provider/build/libs/fineract-provider*.jar /app/fineract-provider.jar

EXPOSE 8080 8443

ENTRYPOINT ["java", \
    "-Duser.home=/tmp", \
    "-Dfile.encoding=UTF-8", \
    "-Duser.timezone=UTC", \
    "-Djava.security.egd=file:/dev/./urandom", \
    "-jar", "/app/fineract-provider.jar"]
