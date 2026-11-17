import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

fun props(path: String) = Properties().apply {
    val f = rootProject.file(path)
    if (!f.exists()) error("$path missing")
    f.inputStream().use { load(it) }
}

// The Idea token (it can only use the Idea page and /api/journey) and the hub's certificate pin, baked in at build time
// (idea_app/secrets.properties, gitignored). Signed with the HomeHub key (android/keystore.properties).
val secrets = props("secrets.properties")
val keystore = props("../android/keystore.properties")

android {
    namespace = "com.homehub.idea"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.homehub.idea"
        minSdk = 29
        targetSdk = 34
        versionCode = 2
        versionName = "1.1"
        ndk { abiFilters += "arm64-v8a" }
        buildConfigField("String", "TOKEN", "\"${secrets.getProperty("token")}\"")
        buildConfigField("String", "PIN", "\"${secrets.getProperty("pin")}\"")
        buildConfigField("String", "WAN", "\"${secrets.getProperty("wan", "")}\"")
    }

    signingConfigs {
        create("release") {
            storeFile = rootProject.file("../android/" + keystore.getProperty("storeFile"))
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
        debug { signingConfig = signingConfigs.getByName("release") }
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
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
