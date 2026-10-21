import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val versionProps = Properties().apply { file("../version.properties").inputStream().use { load(it) } }

android {
    namespace = "app.nori.companion"
    compileSdk = 36

    defaultConfig {
        applicationId = "app.nori.companion"
        minSdk = 29
        targetSdk = 36
        versionCode = versionProps.getProperty("versionCode").trim().toInt()
        versionName = versionProps.getProperty("versionName").trim()
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // A personal, sideloaded build: signed with the debug key so it installs and updates in place without a keystore to look after.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { buildConfig = true }
    testOptions { unitTests.isReturnDefaultValues = true }
    lint { abortOnError = false }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("com.google.android.gms:play-services-location:21.3.0")
    implementation("androidx.health.connect:connect-client:1.1.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("androidx.exifinterface:exifinterface:1.3.7")

    testImplementation("junit:junit:4.13.2")
    // The Android SDK's org.json is only a stub in JVM tests; the real one makes the request/response tests meaningful.
    testImplementation("org.json:json:20240303")
}
