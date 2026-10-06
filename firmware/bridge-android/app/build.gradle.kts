plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "org.openscilab.bridge"
    compileSdk = 34

    defaultConfig {
        applicationId = "org.openscilab.bridge"
        // The DHO800/900 run Android 9 (API 28) - to be confirmed on the instrument (step 3a).
        minSdk = 28
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Signed with the debug key so that firmware/build_all.sh produces an installable APK
            // without a keystore. Replace with a real signing config for public releases.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    buildFeatures {
        buildConfig = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation(project(":core"))
}
