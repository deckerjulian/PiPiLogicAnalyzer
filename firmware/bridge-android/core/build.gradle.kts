// The Android-free part of the bridge: codec, SCPI, commands, cache, pyramid, server.
// Everything here runs on a plain JVM, so it is tested with `./gradlew :core:test`.
// Only Java APIs that Android 9 (API 28) has may be used (no readNBytes, transferTo, Path.of).

plugins {
    id("org.jetbrains.kotlin.jvm")
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    testImplementation(kotlin("test-junit"))
    testImplementation("junit:junit:4.13.2")
}

// The test vectors are shared with the Python driver (openscilab/driver/rigoldho/codec.py).
val sharedVectors = layout.buildDirectory.dir("generated/sharedVectors")
val copySharedVectors by tasks.registering(Copy::class) {
    from(rootDir.resolve("../../tests/fixtures/bridge_codec.json"))
    into(sharedVectors)
}
sourceSets["test"].resources.srcDir(copySharedVectors)
