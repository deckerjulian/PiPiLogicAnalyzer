// openSciLab bridge: the app that runs on the Rigol DHO900 oscilloscope (docs/protocols.md, "Bridge").

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "openscilab-bridge"
include(":core", ":app")
