# Proguard rules for Dapoer Teras Obor Print Service

# Keep Socket.IO classes
-keep class io.socket.** { *; }
-keep class org.json.** { *; }

# Keep OkHttp
-dontwarn okhttp3.**
-keep class okhttp3.** { *; }
-dontwarn okio.**
-keep class okio.** { *; }

# Keep Bluetooth classes
-keep class android.bluetooth.** { *; }

# Keep our activities and service
-keep class com.dapoerterasobor.printservice.PrintService { *; }
-keep class com.dapoerterasobor.printservice.MainActivity { *; }
-keep class com.dapoerterasobor.printservice.SplashActivity { *; }
-keep class com.dapoerterasobor.printservice.BootReceiver { *; }
