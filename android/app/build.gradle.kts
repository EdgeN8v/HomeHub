import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

fun props(name: String) = Properties().apply {
    val f = rootProject.file(name)
    if (!f.exists()) error("android/$name missing: run python tools/gen_secrets.py (and see README for the keystore)")
    f.inputStream().use { load(it) }
}

// Server address, token and certificate pin are baked in at build time (android/secrets.properties, gitignored).
val secrets = props("secrets.properties")
val keystore = props("keystore.properties")

android {
    namespace = "com.homehub.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.homehub.app"
        minSdk = 29
        targetSdk = 34
        versionCode = 12
        versionName = "2.1"
        for (k in listOf("lanUrl", "publicUrl", "token", "pin")) {
            buildConfigField("String", k.uppercase(), "\"${secrets.getProperty(k)}\"")
        }
    }

    signingConfigs {
        create("release") {
            storeFile = rootProject.file(keystore.getProperty("storeFile"))
            storePassword = keystore.getProperty("storePassword")
            keyAlias = keystore.getProperty("keyAlias")
            keyPassword = keystore.getProperty("keyPassword")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("release")
        }
        debug {
            // Same key as release so either build can update the other in place.
            signingConfig = signingConfigs.getByName("release")
        }
    }

    buildFeatures { buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    // Pinned-certificate HTTPS client for the update check and download.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
