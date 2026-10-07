# Stage 1: Build Fineract from source using OpenJDK 17
FROM openjdk:17-slim AS builder

RUN apt-get update && apt-get install -y wget findutils --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /fineract
COPY . .

# Download the gradle wrapper explicitly if missing from source archive
RUN ./gradlew --no-daemon bootJar -x test -x spotlessJavaCheck

# Extract compiled jar content for optimized layering 
WORKDIR /fineract/build/extracted
RUN jar -xf /fineract/fineract-provider/build/libs/fineract-provider.jar

# Download PostgreSQL JDBC driver directly into dependencies tier
WORKDIR /fineract/build/extracted/BOOT-INF/lib
RUN wget -q https://postgresql.org

# Stage 2: Minimal runtime footprint
FROM openjdk:17-slim
WORKDIR /app

COPY --from=builder /fineract/build/extracted/BOOT-INF/lib /app/lib
COPY --from=builder /fineract/build/extracted/META-INF /app/META-INF
COPY --from=builder /fineract/build/extracted/BOOT-INF/classes /app

EXPOSE 8443
ENTRYPOINT ["java", "-cp", "/app:/app/lib/*", "org.apache.fineract.InfrastructureAnonymousApplication"]
