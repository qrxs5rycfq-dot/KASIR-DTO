package com.dapoerterasobor.printservice;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;

/**
 * BootReceiver - Auto-starts PrintService after device boot if it was previously running.
 * Ensures the print service stays active even after device restart.
 */
public class BootReceiver extends BroadcastReceiver {

    private static final String TAG = "BootReceiver";
    private static final String PREFS_NAME = "PrintServicePrefs";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || intent.getAction() == null) {
            return;
        }

        String action = intent.getAction();
        Log.d(TAG, "Received action: " + action);

        // Handle various boot completed actions
        if (Intent.ACTION_BOOT_COMPLETED.equals(action) ||
                Intent.ACTION_LOCKED_BOOT_COMPLETED.equals(action) ||
                Intent.ACTION_MY_PACKAGE_REPLACED.equals(action) ||
                "android.intent.action.QUICKBOOT_POWERON".equals(action) ||
                "com.htc.intent.action.QUICKBOOT_POWERON".equals(action)) {

            // Delay start to let system settle (especially after boot)
            startPrintServiceWithDelay(context);
        }
    }

    private void startPrintServiceWithDelay(Context context) {
        // Use a delay to ensure system is ready
        new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(() -> {
            try {
                SharedPreferences prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
                boolean wasRunning = prefs.getBoolean("service_running", false);

                if (wasRunning) {
                    Log.i(TAG, "Restarting PrintService (was running before)...");

                    // Check if we have necessary permissions
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                        // On Android 12+, we need to check Bluetooth permissions
                        // but we can't check permissions in receiver, so we just attempt to start
                        Log.d(TAG, "Android 12+, attempting to start service...");
                    }

                    Intent serviceIntent = new Intent(context, PrintService.class);

                    try {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            context.startForegroundService(serviceIntent);
                        } else {
                            context.startService(serviceIntent);
                        }
                        Log.i(TAG, "PrintService start initiated");
                    } catch (Exception e) {
                        Log.e(TAG, "Failed to start PrintService: " + e.getMessage());

                        // Try one more time after longer delay
                        retryStartService(context);
                    }
                } else {
                    Log.d(TAG, "PrintService was not running before boot, skipping auto-start");
                }
            } catch (Exception e) {
                Log.e(TAG, "Error in BootReceiver: " + e.getMessage());
            }
        }, 3000); // 3 second delay
    }

    private void retryStartService(Context context) {
        new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(() -> {
            try {
                Log.i(TAG, "Retrying to start PrintService...");
                Intent serviceIntent = new Intent(context, PrintService.class);

                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(serviceIntent);
                } else {
                    context.startService(serviceIntent);
                }
                Log.i(TAG, "PrintService start retry initiated");
            } catch (Exception e) {
                Log.e(TAG, "Retry failed: " + e.getMessage());
            }
        }, 10000); // 10 second retry delay
    }
}