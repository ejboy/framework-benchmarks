# Customize JVM settings for benchmark runs

Start community experiments in a fork. For controlled measurements, run the
baseline and variant on the same dedicated host and guest. The
[accepted Debian 13 report](evidence/debian-v1/report.html) shows the published setup.

## Run from a GitHub fork

1. Open [PVRLabs/framework-benchmarks](https://github.com/PVRLabs/framework-benchmarks)
   and click **Fork**.
2. Open your fork's **Actions** tab and enable workflows if prompted.
3. In your fork, open `framework-benchmarks/config-v1.json`, click the pencil
   icon, edit the `jvm` array, and commit the change in GitHub. A push changing
   that file triggers the [JVM Benchmark workflow](../.github/workflows/benchmark.yml)
   on a GitHub-hosted Ubuntu runner. The workflow provisions the Debian guest,
   checks correctness, and runs the preparation and measurement suites.
4. Open **Actions → JVM Benchmark** and select your run. The
   **Run preparation and benchmark measurements** step prints a results table.
   Under **Artifacts**, download `benchmark-results-<run number>-<attempt>`.
   Extract the archive and open `measurement/report.html` or
   `measurement/featured.svg`. Keep the manifest and evidence directories
   alongside them so the report links work.

[Correctness CI](../.github/workflows/ci.yml) runs separately. GitHub-hosted
measurements are for exploration. Use dedicated hardware for comparisons
against the published measurements.

## Try the four community-suggested flags

Upstream [config-v1.json](config-v1.json) contains the frozen v1 JVM settings.
Changes to the official protocol should introduce a new version.

For exploratory runs, edit `config-v1.json` directly in your fork and run the
suite without `--config`, or pass a separate configuration with `--config`.
For the separate-file option, copy the default and replace its `jvm` array
with the array below.

```sh
mkdir -p work
cp framework-benchmarks/config-v1.json work/v1-jvm.json
```

```json
[
  "-Xms16m",
  "-Xmx80m",
  "-XX:+UseG1GC",
  "-Duser.timezone=UTC",
  "-XX:TrimNativeHeapInterval=10000",
  "-XX:MinHeapFreeRatio=5",
  "-XX:MaxHeapFreeRatio=10",
  "-XX:G1PeriodicGCInterval=10000"
]
```

Check the flags with `-XX:+PrintFlagsFinal -version` on the guest's Java
executable before running the suite. `TrimNativeHeapInterval` requires
HotSpot on Linux with glibc. `G1PeriodicGCInterval` requires G1. Both intervals
are in milliseconds. The free ratios are percentages used when resizing the heap.
[OpenJDK Java options](https://github.com/openjdk/jdk/blob/master/src/java.base/share/man/java.md)
· [JEP 346](https://openjdk.org/jeps/346)

## Run locally on dedicated hardware

1. Follow the [VM setup and correctness instructions](../README.md#vm-setup)
   and the [v1 suite guide](suite-v1.md). Use the pinned Debian image,
   PostgreSQL package, and HotSpot build. Record the source commit, build
   toolchain, and JAR and dependency hashes. Check the application versions
   against the accepted report and its artifact manifests if reproducing the
   published comparison.
2. Use the suite guide's bundle preparation and run commands with the stock
   JVM flags, `-Xms16m -Xmx80m -XX:+UseG1GC -Duser.timezone=UTC`. Use
   `--preparation --output work/v1-baseline-prep` for the short smoke suite.
   Review all four candidates in both rounds, including cleanup. Then drop
   `--preparation` and use `--output work/v1-baseline` for measurement.

Reuse the four guest bundles from the baseline. They must include the guest
runner from this checkout. Run a short preparation suite first.

```sh
python3 framework-benchmarks/scripts/colocated-suite.py run --vm fb-debian-v1 \
  --guest-run-root /var/tmp/fb-runs \
  --config work/v1-jvm.json --preparation --output work/v1-jvm-prep \
  --bundle jex-coilbase=/var/tmp/fb-v1-review/jex-coilbase \
  --bundle quarkus-coilbase=/var/tmp/fb-v1-review/quarkus-coilbase \
  --bundle spring-boot=/var/tmp/fb-v1-review/spring-boot \
  --bundle micronaut-data-jdbc=/var/tmp/fb-v1-review/micronaut-data-jdbc
```

Check each candidate's `guest/launch.json` and `guest/effective-jvm-flags.txt`.
For measurement, drop `--preparation` and use `--output work/v1-jvm`.
The suite labels a run as custom when its JVM settings differ from the frozen
values, whether you edited the fork's file or used `--config`. The other two
fields are `java_executable`, an absolute path inside the guest, and `jdk_build`,
the expected build identifier from that executable's `-version` output.

## Compare results

Reuse the same physical host, guest image and resources, kernel, storage,
PostgreSQL settings, pool size, and JDK build. Match host activity and guest
boot and cache conditions as closely as possible. Compare application JAR and
dependency hashes in `environment/artifacts.txt` and verify the workload and
synthetic replay hashes in `inputs.json`. Keep the database reset and seed,
replay cadence, request mix and schedule, timeouts, phase lengths, and
two-round candidate order from the [v1 protocol](protocol-v1.md).

The suite writes `report.html` alongside `suite.json` and a `featured.svg`
for a complete measurement suite. The manifest embeds the effective
JVM settings under `protocol`. The manifest's `configuration_sha256` covers
only `java_executable`, `jdk_build`, and `jvm`, serialized as compact JSON
with sorted keys and non-ASCII characters escaped. Custom charts show the
first 12 characters of this hash.
Keep the entire evidence directory together to regenerate its report offline.

```sh
python3 framework-benchmarks/scripts/colocated-suite.py report work/v1-baseline/suite.json
python3 framework-benchmarks/scripts/colocated-suite.py report work/v1-jvm/suite.json
```

Custom reports use the effective settings from their manifest. To regenerate the
accepted report, use the [README's copy-and-compare command](../README.md#regenerate-the-featured-report-and-chart).
Keep experiment outputs under fresh `work/`
paths and leave the accepted evidence directories and README chart in place.

Compare each stack in both rounds against the local baseline. First check
acceptance, achieved throughput, errors, timeouts, unsent requests, and OOMs.
Then compare readiness, sustained p95 and p99, peak application RSS, and
minimum guest MemAvailable. The four-flag example measures their combined
effect. Run separate, labeled experiments if you want to isolate individual
flags.

## Share results

Share findings in a
[GitHub issue](https://github.com/PVRLabs/framework-benchmarks/issues) or
propose a reproducible change through a
[pull request](https://github.com/PVRLabs/framework-benchmarks/pulls).
Include the source commit and configuration diff, build and run commands,
host and guest details, artifact and input hashes, and baseline and experiment
reports or links to the raw evidence. Summarize the per-round measurements,
any failed runs or deviations, and the conclusions the data supports.

Alternative runtimes such as GraalVM Native Image are possible future extensions.
