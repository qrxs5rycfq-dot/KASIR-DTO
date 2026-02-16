package com.dapoerterasobor.printservice;

import android.annotation.SuppressLint;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.WindowManager;
import android.widget.ProgressBar;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatActivity;

/**
 * SplashActivity - Splash screen with brand logo.
 * Shows for 2 seconds then launches MainActivity.
 * Also handles initial configuration check.
 */
@SuppressLint("CustomSplashScreen")
public class SplashActivity extends AppCompatActivity {

    private static final String TAG = "SplashActivity";
    private static final int SPLASH_DURATION_MS = 2000;
    private static final String PREFS_NAME = "PrintServicePrefs";
    private static final String DEFAULT_SERVER_URL = "http://10.111.108.37:8000";

    private ProgressBar progressBar;
    private TextView txtStatus;
    private Handler handler = new Handler(Looper.getMainLooper());
    private boolean isActivityActive = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // Enable edge-to-edge display
        getWindow().setFlags(
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
        );

        setContentView(R.layout.activity_splash);

        // Initialize views
        progressBar = findViewById(R.id.progressBar);
        txtStatus = findViewById(R.id.txtSplashStatus);

        isActivityActive = true;

        // Start splash sequence
        startSplashSequence();
    }

    private void startSplashSequence() {
        // Show initial status
        updateStatus("Memuat...");

        // Check configuration in background
        checkConfiguration();

        // Navigate to main after splash duration
        handler.postDelayed(this::navigateToMain, SPLASH_DURATION_MS);
    }

    private void checkConfiguration() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String serverUrl = prefs.getString("server_url", DEFAULT_SERVER_URL);

        if (serverUrl.isEmpty()) {
            updateStatus("🔧 Menunggu konfigurasi...");
        } else {
            // Check if service was running
            boolean serviceRunning = prefs.getBoolean("service_running", false);
            if (serviceRunning) {
                updateStatus("🔄 Print Service akan dimulai...");
            } else {
                updateStatus("✅ Siap digunakan");
            }
        }
    }

    private void updateStatus(String status) {
        runOnUiThread(() -> {
            if (txtStatus != null && isActivityActive) {
                txtStatus.setText(status);
                txtStatus.setVisibility(View.VISIBLE);
            }
        });
    }

    private void navigateToMain() {
        if (!isActivityActive) return;

        try {
            Intent intent = new Intent(SplashActivity.this, MainActivity.class);
            startActivity(intent);
            finish();

            // Smooth transition
            overridePendingTransition(android.R.anim.fade_in, android.R.anim.fade_out);
        } catch (Exception e) {
            e.printStackTrace();
            // Fallback if navigation fails
            finish();
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        // Cancel any pending navigation if activity is paused
        handler.removeCallbacksAndMessages(null);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        isActivityActive = false;
        handler.removeCallbacksAndMessages(null);
    }
}