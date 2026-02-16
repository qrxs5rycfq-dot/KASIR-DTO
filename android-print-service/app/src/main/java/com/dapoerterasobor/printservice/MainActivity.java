package com.dapoerterasobor.printservice;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.DownloadManager;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.util.Log;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.GeolocationPermissions;
import android.webkit.JavascriptInterface;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.RadioGroup;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import androidx.activity.result.ActivityResultLauncher;
import androidx.activity.result.contract.ActivityResultContracts;
import androidx.annotation.NonNull;
import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.ContextCompat;
import androidx.core.content.FileProvider;
import androidx.localbroadcastmanager.content.LocalBroadcastManager;
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

import java.io.File;
import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public class MainActivity extends AppCompatActivity {

    private static final String TAG = "MainActivity";
    private static final int PERMISSION_REQUEST_CODE = 100;
    private static final String PREFS_NAME = "PrintServicePrefs";
    private static final String DEFAULT_SERVER_URL = "http://10.111.108.37:8000";

    // Timeouts
    private static final int SERVICE_STATUS_UPDATE_INTERVAL = 2000; // 2 detik
    private static final int WEBVIEW_HEALTH_CHECK_INTERVAL = 30000; // 30 detik
    private static final int NETWORK_RELOAD_DELAY = 2000; // 2 detik setelah network kembali
    private static final int ERROR_AUTO_RETRY_INTERVAL = 5000; // 5 detik auto-retry saat error

    private WebView webView;
    private SwipeRefreshLayout swipeRefreshLayout;
    private View settingsPanel;
    private EditText editServerUrl;
    private EditText editUsername;
    private EditText editPassword;
    private EditText editLanAddress;
    private Spinner spinnerPrinter;
    private Spinner spinnerPaperWidth;
    private TextView txtServiceStatus;
    private TextView txtPrintStats;
    private Button btnToggleService;
    private Button btnRefreshPrinters;
    private RadioGroup radioPrinterType;
    private LinearLayout sectionBluetooth;
    private LinearLayout sectionLan;

    private boolean settingsVisible = false;
    private final AtomicBoolean isServiceRunning = new AtomicBoolean(false);
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService backgroundExecutor = Executors.newSingleThreadExecutor();

    // File upload handling
    private ValueCallback<Uri[]> fileUploadCallback;
    private Uri cameraImageUri;

    // Network monitoring
    private ConnectivityManager connectivityManager;
    private ConnectivityManager.NetworkCallback networkCallback;
    private boolean isNetworkAvailable = true;
    private boolean isNetworkValidated = false;
    private boolean hasWebViewError = false;
    private final Runnable autoRetryRunnable = this::retryLoadPage;
    private final Runnable networkReloadRunnable = this::reloadWebViewAfterNetworkChange;

    // Activity result launchers
    private ActivityResultLauncher<Intent> fileChooserLauncher;
    private ActivityResultLauncher<String[]> permissionLauncher;

    // Broadcast receiver untuk status service
    private final BroadcastReceiver serviceStatusReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (intent != null) {
                String action = intent.getAction();
                if ("com.dapoerterasobor.PRINT_SERVICE_STATUS".equals(action)) {
                    boolean isRunning = intent.getBooleanExtra("is_running", false);
                    int printed = intent.getIntExtra("printed_count", 0);
                    int failed = intent.getIntExtra("failed_count", 0);
                    String status = intent.getStringExtra("status");

                    updateServiceStatus(isRunning, printed, failed, status);
                }
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        Log.i(TAG, "MainActivity created");

        // Register activity result launchers
        registerLaunchers();

        // Register broadcast receiver
        registerServiceStatusReceiver();

        // Initialize views
        initializeViews();

        // Request permissions
        requestAppPermissions();

        // Load saved settings
        loadSettings();

        // Setup WebView with full POS capabilities
        setupWebView();

        // Setup buttons
        setupButtons();

        // Load paired printers
        loadPairedPrinters();

        // Start periodic status updates
        startServiceStatusMonitor();

        // Setup network change monitoring for WebView stability
        setupNetworkMonitoring();

        // Start WebView health check
        startWebViewHealthCheck();
    }

    private void initializeViews() {
        webView = findViewById(R.id.webView);
        swipeRefreshLayout = findViewById(R.id.swipeRefreshLayout);
        settingsPanel = findViewById(R.id.settingsPanel);
        editServerUrl = findViewById(R.id.editServerUrl);
        editUsername = findViewById(R.id.editUsername);
        editPassword = findViewById(R.id.editPassword);
        editLanAddress = findViewById(R.id.editLanAddress);
        spinnerPrinter = findViewById(R.id.spinnerPrinter);
        txtServiceStatus = findViewById(R.id.txtServiceStatus);
        txtPrintStats = findViewById(R.id.txtPrintStats);
        btnToggleService = findViewById(R.id.btnToggleService);
        btnRefreshPrinters = findViewById(R.id.btnRefreshPrinters);
        radioPrinterType = findViewById(R.id.radioPrinterType);
        sectionBluetooth = findViewById(R.id.sectionBluetooth);
        sectionLan = findViewById(R.id.sectionLan);
        spinnerPaperWidth = findViewById(R.id.spinnerPaperWidth);

        // Setup paper width spinner
        String[] paperWidths = {"80mm (Standar)", "60mm (Sedang)", "40mm (Kecil)"};
        ArrayAdapter<String> paperAdapter = new ArrayAdapter<>(this,
                android.R.layout.simple_spinner_item, paperWidths);
        paperAdapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        spinnerPaperWidth.setAdapter(paperAdapter);

        // Sembunyikan stats jika belum ada data
        if (txtPrintStats != null) {
            txtPrintStats.setVisibility(View.GONE);
        }
    }

    private void setupButtons() {
        findViewById(R.id.btnSettings).setOnClickListener(v -> toggleSettings());
        findViewById(R.id.btnSave).setOnClickListener(v -> saveSettings());
        btnRefreshPrinters.setOnClickListener(v -> loadPairedPrinters());
        btnToggleService.setOnClickListener(v -> togglePrintService());

        // Printer type toggle
        radioPrinterType.setOnCheckedChangeListener((group, checkedId) -> {
            if (checkedId == R.id.radioBluetooth) {
                sectionBluetooth.setVisibility(View.VISIBLE);
                sectionLan.setVisibility(View.GONE);
                validatePrinterSelection();
            } else {
                sectionBluetooth.setVisibility(View.GONE);
                sectionLan.setVisibility(View.VISIBLE);
                validateLanAddress();
            }
        });
    }

    private void registerLaunchers() {
        // File chooser launcher (for camera + file picker)
        fileChooserLauncher = registerForActivityResult(
                new ActivityResultContracts.StartActivityForResult(),
                result -> {
                    if (fileUploadCallback == null) return;

                    Uri[] results = null;
                    if (result.getResultCode() == RESULT_OK) {
                        if (result.getData() != null) {
                            // File picker result
                            String dataString = result.getData().getDataString();
                            if (dataString != null) {
                                results = new Uri[]{Uri.parse(dataString)};
                            }
                        } else if (cameraImageUri != null) {
                            // Camera result
                            results = new Uri[]{cameraImageUri};
                        }
                    }

                    fileUploadCallback.onReceiveValue(results);
                    fileUploadCallback = null;
                    cameraImageUri = null;
                }
        );

        // Permission launcher
        permissionLauncher = registerForActivityResult(
                new ActivityResultContracts.RequestMultiplePermissions(),
                result -> {
                    Log.i(TAG, "Permissions result: " + result);

                    boolean allGranted = true;
                    for (Boolean granted : result.values()) {
                        if (!granted) {
                            allGranted = false;
                            break;
                        }
                    }

                    if (allGranted) {
                        Toast.makeText(this, "Semua izin diberikan", Toast.LENGTH_SHORT).show();
                        loadPairedPrinters();
                    } else {
                        showPermissionDeniedDialog();
                    }
                }
        );
    }

    private void registerServiceStatusReceiver() {
        IntentFilter filter = new IntentFilter("com.dapoerterasobor.PRINT_SERVICE_STATUS");
        LocalBroadcastManager.getInstance(this).registerReceiver(serviceStatusReceiver, filter);
    }

    private void startServiceStatusMonitor() {
        mainHandler.postDelayed(new Runnable() {
            @Override
            public void run() {
                updateServiceStatusFromPrefs();
                mainHandler.postDelayed(this, SERVICE_STATUS_UPDATE_INTERVAL);
            }
        }, SERVICE_STATUS_UPDATE_INTERVAL);
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void setupWebView() {
        WebSettings webSettings = webView.getSettings();

        // Core WebView settings
        webSettings.setJavaScriptEnabled(true);
        webSettings.setDomStorageEnabled(true);
        webSettings.setDatabaseEnabled(true);
        webSettings.setCacheMode(WebSettings.LOAD_DEFAULT);
        webSettings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        webSettings.setAllowFileAccess(true);
        webSettings.setAllowContentAccess(true);
        webSettings.setMediaPlaybackRequiresUserGesture(false);
        webSettings.setLoadsImagesAutomatically(true);
        webSettings.setSupportZoom(true);
        webSettings.setBuiltInZoomControls(true);
        webSettings.setDisplayZoomControls(false);
        webSettings.setUseWideViewPort(true);
        webSettings.setLoadWithOverviewMode(true);
        webSettings.setDefaultTextEncodingName("UTF-8");

        // User agent
        String defaultUA = webSettings.getUserAgentString();
        webSettings.setUserAgentString(defaultUA + " DapoerTerasOborPOS/2.0 Android");

        // Enable cookies
        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);
        cookieManager.setAcceptThirdPartyCookies(webView, true);

        // JavaScript bridge
        webView.addJavascriptInterface(new PrintBridge(), "AndroidPrint");

        // Setup Swipe Refresh
        setupSwipeRefresh();

        // WebViewClient
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                if (url.startsWith("tel:") || url.startsWith("mailto:") || url.startsWith("whatsapp:")) {
                    try {
                        Intent intent = new Intent(Intent.ACTION_VIEW, request.getUrl());
                        startActivity(intent);
                    } catch (Exception e) {
                        Log.e(TAG, "Error opening external link: " + e.getMessage());
                    }
                    return true;
                }
                return false;
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                super.onPageStarted(view, url, favicon);
                Log.d(TAG, "Page loading: " + url);
                // Cancel auto-retry saat halaman mulai dimuat
                mainHandler.removeCallbacks(autoRetryRunnable);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                // Hentikan indikator refresh ketika halaman selesai dimuat
                if (swipeRefreshLayout != null && swipeRefreshLayout.isRefreshing()) {
                    swipeRefreshLayout.setRefreshing(false);
                }
                // Reset error state saat halaman berhasil dimuat
                if (!url.startsWith("data:")) {
                    hasWebViewError = false;
                    mainHandler.removeCallbacks(autoRetryRunnable);
                }
                Log.d(TAG, "Page finished: " + url);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                // Hanya handle error untuk main frame (bukan sub-resources)
                if (request.isForMainFrame()) {
                    int errorCode = error.getErrorCode();
                    String description = error.getDescription().toString();
                    Log.e(TAG, "WebView error: " + errorCode + " - " + description);
                    handlePageLoadError(description);
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse errorResponse) {
                // Handle HTTP errors (502, 503, 504, dll) hanya untuk main frame
                if (request.isForMainFrame()) {
                    int statusCode = errorResponse.getStatusCode();
                    if (statusCode >= 500) {
                        Log.e(TAG, "HTTP error: " + statusCode + " for " + request.getUrl());
                        handlePageLoadError("Server error: " + statusCode);
                    }
                }
            }
        });

        // WebChromeClient
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView webView,
                                             ValueCallback<Uri[]> filePathCallback,
                                             FileChooserParams fileChooserParams) {
                return handleFileChooser(filePathCallback, fileChooserParams);
            }

            @Override
            public void onGeolocationPermissionsShowPrompt(String origin,
                                                           GeolocationPermissions.Callback callback) {
                handleGeolocationPermission(origin, callback);
            }
        });

        // Download listener
        webView.setDownloadListener((url, userAgent, contentDisposition, mimetype, contentLength) -> {
            handleDownload(url, userAgent, contentDisposition, mimetype, contentLength);
        });

        // Load URL
        loadWebViewUrl();
    }

    private void setupSwipeRefresh() {
        // Nonaktifkan swipe refresh sepenuhnya untuk menghindari konflik dengan scroll
        swipeRefreshLayout.setEnabled(false);
    }

    private boolean handleFileChooser(ValueCallback<Uri[]> filePathCallback,
                                      WebChromeClient.FileChooserParams fileChooserParams) {
        // Cancel existing callback
        if (fileUploadCallback != null) {
            fileUploadCallback.onReceiveValue(null);
        }
        fileUploadCallback = filePathCallback;

        try {
            // Determine accepted types
            String[] acceptTypes = fileChooserParams.getAcceptTypes();
            boolean acceptsImage = false;
            boolean acceptsVideo = false;

            for (String type : acceptTypes) {
                if (type != null) {
                    if (type.startsWith("image/") || type.equals("image/*")) {
                        acceptsImage = true;
                    } else if (type.startsWith("video/") || type.equals("video/*")) {
                        acceptsVideo = true;
                    }
                }
            }

            Intent chooserIntent;

            if (acceptsImage) {
                chooserIntent = createImagePickerIntent();
            } else if (acceptsVideo) {
                chooserIntent = createVideoPickerIntent();
            } else {
                chooserIntent = createFilePickerIntent(fileChooserParams);
            }

            if (chooserIntent != null) {
                fileChooserLauncher.launch(chooserIntent);
                return true;
            }
        } catch (Exception e) {
            Log.e(TAG, "Error in file chooser: " + e.getMessage());
        }

        fileUploadCallback.onReceiveValue(null);
        fileUploadCallback = null;
        return false;
    }

    private Intent createImagePickerIntent() {
        Intent chooserIntent;

        if (checkCameraPermission()) {
            Intent cameraIntent = createCameraIntent();
            Intent galleryIntent = new Intent(Intent.ACTION_GET_CONTENT);
            galleryIntent.setType("image/*");
            galleryIntent.addCategory(Intent.CATEGORY_OPENABLE);

            chooserIntent = Intent.createChooser(galleryIntent, "Pilih Gambar");
            if (cameraIntent != null) {
                chooserIntent.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[]{cameraIntent});
            }
        } else {
            // Only gallery if no camera permission
            chooserIntent = new Intent(Intent.ACTION_GET_CONTENT);
            chooserIntent.setType("image/*");
            chooserIntent.addCategory(Intent.CATEGORY_OPENABLE);
            chooserIntent = Intent.createChooser(chooserIntent, "Pilih Gambar");
        }

        return chooserIntent;
    }

    private Intent createVideoPickerIntent() {
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.setType("video/*");
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        return Intent.createChooser(intent, "Pilih Video");
    }

    private Intent createFilePickerIntent(WebChromeClient.FileChooserParams params) {
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.setType("*/*");
        intent.addCategory(Intent.CATEGORY_OPENABLE);

        if (params.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE) {
            intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        }

        return Intent.createChooser(intent, "Pilih File");
    }

    private boolean checkCameraPermission() {
        return ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED;
    }

    private Intent createCameraIntent() {
        try {
            Intent cameraIntent = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
            if (cameraIntent.resolveActivity(getPackageManager()) != null) {
                File photoFile = createImageFile();
                if (photoFile != null) {
                    cameraImageUri = FileProvider.getUriForFile(this,
                            getPackageName() + ".fileprovider", photoFile);
                    cameraIntent.putExtra(MediaStore.EXTRA_OUTPUT, cameraImageUri);
                    return cameraIntent;
                }
            }
        } catch (Exception e) {
            Log.e(TAG, "Camera intent error: " + e.getMessage());
        }
        return null;
    }

    private File createImageFile() throws IOException {
        String timeStamp = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.getDefault()).format(new Date());
        String imageFileName = "POS_" + timeStamp + "_";
        File storageDir = getExternalCacheDir();
        return File.createTempFile(imageFileName, ".jpg", storageDir);
    }

    private void handleGeolocationPermission(String origin, GeolocationPermissions.Callback callback) {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                == PackageManager.PERMISSION_GRANTED) {
            callback.invoke(origin, true, false);
        } else {
            // Request permission
            permissionLauncher.launch(new String[]{Manifest.permission.ACCESS_FINE_LOCATION});
            callback.invoke(origin, false, false);
        }
    }

    private void handleDownload(String url, String userAgent, String contentDisposition,
                                String mimetype, long contentLength) {
        try {
            DownloadManager.Request request = new DownloadManager.Request(Uri.parse(url));
            String filename = URLUtil.guessFileName(url, contentDisposition, mimetype);

            request.setTitle(filename);
            request.setDescription("Mengunduh " + filename);
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename);
            request.setAllowedOverMetered(true);
            request.setAllowedOverRoaming(true);

            // Add cookies for authenticated downloads
            String cookies = CookieManager.getInstance().getCookie(url);
            if (cookies != null) {
                request.addRequestHeader("Cookie", cookies);
            }

            DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            if (dm != null) {
                dm.enqueue(request);
                showToast("Mengunduh: " + filename);
            }
        } catch (Exception e) {
            Log.e(TAG, "Download error: " + e.getMessage());
            showToast("Gagal mengunduh file");
        }
    }

    private void handlePageLoadError(String error) {
        hasWebViewError = true;
        // Hentikan refresh indicator
        if (swipeRefreshLayout != null && swipeRefreshLayout.isRefreshing()) {
            swipeRefreshLayout.setRefreshing(false);
        }
        // Sanitize error message untuk HTML display
        String safeError = error.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\"", "&quot;").replace("'", "&#39;");
        // Tampilkan error page dengan auto-retry
        String serverUrl = getServerUrl();
        // Validasi URL scheme untuk keamanan
        if (!serverUrl.startsWith("http://") && !serverUrl.startsWith("https://")) {
            serverUrl = "http://" + serverUrl;
        }
        String safeUrl = serverUrl.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\"", "&quot;").replace("'", "&#39;");
        runOnUiThread(() -> {
            webView.loadDataWithBaseURL(null,
                    "<html><body style='background:#0f172a;color:white;text-align:center;padding-top:30%;font-family:sans-serif;'>"
                            + "<h2>⚠️ Tidak Dapat Terhubung</h2>"
                            + "<p style='color:#94a3b8;margin:10px 20px;'>" + safeError + "</p>"
                            + "<p style='color:#64748b;font-size:14px;'>Mencoba ulang otomatis...</p>"
                            + "<button onclick='window.location.href=\"" + safeUrl + "\"' "
                            + "style='margin-top:20px;padding:12px 32px;background:#3b82f6;color:white;"
                            + "border:none;border-radius:8px;font-size:16px;cursor:pointer;'>🔄 Coba Lagi</button>"
                            + "<p style='color:#475569;font-size:12px;margin-top:20px;'>Server: " + safeUrl + "</p>"
                            + "</body></html>",
                    "text/html", "UTF-8", null);
        });
        // Schedule auto-retry
        mainHandler.removeCallbacks(autoRetryRunnable);
        mainHandler.postDelayed(autoRetryRunnable, ERROR_AUTO_RETRY_INTERVAL);
    }

    private void retryLoadPage() {
        if (!hasWebViewError) return;
        String serverUrl = getServerUrl();
        if (serverUrl.isEmpty()) return;

        Log.i(TAG, "Auto-retrying page load: " + serverUrl);
        runOnUiThread(() -> {
            if (webView != null) {
                webView.loadUrl(serverUrl);
            }
        });
    }

    // ==================== Network Monitoring ====================

    private void setupNetworkMonitoring() {
        connectivityManager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (connectivityManager == null) return;

        // Check initial network state
        NetworkCapabilities caps = connectivityManager.getNetworkCapabilities(
                connectivityManager.getActiveNetwork());
        isNetworkAvailable = caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        isNetworkValidated = caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);

        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(@NonNull Network network) {
                Log.i(TAG, "Network available");
                boolean wasUnavailable = !isNetworkAvailable;
                isNetworkAvailable = true;

                if (wasUnavailable || hasWebViewError) {
                    scheduleNetworkReload();
                }
            }

            @Override
            public void onLost(@NonNull Network network) {
                Log.w(TAG, "Network lost");
                isNetworkAvailable = false;
                isNetworkValidated = false;
            }

            @Override
            public void onCapabilitiesChanged(@NonNull Network network,
                                               @NonNull NetworkCapabilities capabilities) {
                boolean nowValidated = capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                        && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED);
                boolean wasNotValidated = !isNetworkValidated;
                isNetworkValidated = nowValidated;

                // Hanya reload saat transisi dari non-validated ke validated
                if (nowValidated && wasNotValidated && hasWebViewError) {
                    scheduleNetworkReload();
                }
            }
        };

        NetworkRequest networkRequest = new NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build();
        connectivityManager.registerNetworkCallback(networkRequest, networkCallback);
    }

    private void scheduleNetworkReload() {
        // Hapus reload sebelumnya untuk menghindari duplikasi
        mainHandler.removeCallbacks(networkReloadRunnable);
        mainHandler.postDelayed(networkReloadRunnable, NETWORK_RELOAD_DELAY);
    }

    private void reloadWebViewAfterNetworkChange() {
        if (hasWebViewError && webView != null) {
            Log.i(TAG, "Network restored — reloading WebView");
            webView.clearCache(false);
            retryLoadPage();
        }
    }

    // ==================== WebView Health Check ====================

    private void startWebViewHealthCheck() {
        mainHandler.postDelayed(new Runnable() {
            @Override
            public void run() {
                checkWebViewHealth();
                mainHandler.postDelayed(this, WEBVIEW_HEALTH_CHECK_INTERVAL);
            }
        }, WEBVIEW_HEALTH_CHECK_INTERVAL);
    }

    private void checkWebViewHealth() {
        if (webView == null || hasWebViewError) return;

        String currentUrl = webView.getUrl();
        // Skip health check jika WebView menampilkan halaman internal
        if (currentUrl == null || currentUrl.startsWith("data:") || currentUrl.equals("about:blank")) {
            return;
        }

        // Evaluasi JavaScript sederhana untuk cek apakah WebView masih responsif
        webView.evaluateJavascript("(function() { return document.readyState; })()",
                value -> {
                    if (value == null || value.equals("null")) {
                        Log.w(TAG, "WebView health check: unresponsive, reloading");
                        hasWebViewError = true;
                        retryLoadPage();
                    }
                });
    }

    private void loadWebViewUrl() {
        String serverUrl = getServerUrl();
        if (!serverUrl.isEmpty()) {
            webView.loadUrl(serverUrl);
        } else {
            webView.loadDataWithBaseURL(null,
                    "<html><body style='background:#0f172a;color:white;text-align:center;padding-top:40%;font-family:sans-serif;'>"
                            + "<h2>🖨️ Dapoer Teras Obor POS</h2>"
                            + "<p style='color:#94a3b8;'>Tekan ⚙️ untuk mengatur URL server</p>"
                            + "</body></html>",
                    "text/html", "UTF-8", null
            );
            // Pastikan indikator refresh tidak stuck jika load halaman default
            if (swipeRefreshLayout != null && swipeRefreshLayout.isRefreshing()) {
                swipeRefreshLayout.setRefreshing(false);
            }
        }
    }

    // JavaScript Bridge
    private class PrintBridge {
        @JavascriptInterface
        public boolean isServiceRunning() {
            return isServiceRunning.get();
        }

        @JavascriptInterface
        public String getPrinterName() {
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            String printerName = prefs.getString("printer_name", null);
            if (printerName == null) {
                String printerAddress = prefs.getString("printer_address", "");
                if (!printerAddress.isEmpty()) {
                    return "Printer: " + printerAddress;
                }
            }
            return printerName != null ? printerName : "Not configured";
        }

        @JavascriptInterface
        public String getPrinterType() {
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            return prefs.getString("printer_type", "bluetooth");
        }

        @JavascriptInterface
        public int getPrintedCount() {
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            return prefs.getInt("printed_count", 0);
        }

        @JavascriptInterface
        public int getFailedCount() {
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            return prefs.getInt("failed_count", 0);
        }

        @JavascriptInterface
        public void openSettings() {
            runOnUiThread(() -> {
                settingsVisible = true;
                settingsPanel.setVisibility(View.VISIBLE);
            });
        }

        @JavascriptInterface
        public String getAppVersion() {
            return "2.0.0";
        }

        @JavascriptInterface
        public void showToast(String message) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this, message, Toast.LENGTH_SHORT).show());
        }

        @JavascriptInterface
        public void printTest() {
            runOnUiThread(() -> {
                // Trigger test print
                Intent intent = new Intent(MainActivity.this, PrintService.class);
                intent.setAction("TEST_PRINT");
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    startForegroundService(intent);
                } else {
                    startService(intent);
                }
            });
        }
    }

    // Settings Management
    private void toggleSettings() {
        settingsVisible = !settingsVisible;
        settingsPanel.setVisibility(settingsVisible ? View.VISIBLE : View.GONE);

        if (settingsVisible) {
            loadSettings();
            loadPairedPrinters();
        }
    }

    private void loadSettings() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        editServerUrl.setText(prefs.getString("server_url", DEFAULT_SERVER_URL));
        editUsername.setText(prefs.getString("username", "admin"));
        editPassword.setText(prefs.getString("password", "Asecc123@"));
        editLanAddress.setText(prefs.getString("lan_printer_address", "10.111.108.37:9100"));

        String printerType = prefs.getString("printer_type", "bluetooth");
        if ("lan".equals(printerType)) {
            radioPrinterType.check(R.id.radioLan);
            sectionBluetooth.setVisibility(View.GONE);
            sectionLan.setVisibility(View.VISIBLE);
        } else {
            radioPrinterType.check(R.id.radioBluetooth);
            sectionBluetooth.setVisibility(View.VISIBLE);
            sectionLan.setVisibility(View.GONE);
        }

        // Load paper width setting
        int paperWidth = prefs.getInt("paper_width", 80);
        switch (paperWidth) {
            case 60: spinnerPaperWidth.setSelection(1); break;
            case 40: spinnerPaperWidth.setSelection(2); break;
            default: spinnerPaperWidth.setSelection(0); break; // 80mm
        }
    }

    private void saveSettings() {
        String serverUrl = editServerUrl.getText().toString().trim();
        String username = editUsername.getText().toString().trim();
        String password = editPassword.getText().toString().trim();

        // Validasi
        if (serverUrl.isEmpty()) {
            showToast("URL Server tidak boleh kosong");
            return;
        }

        // Format URL
        if (!serverUrl.startsWith("http://") && !serverUrl.startsWith("https://")) {
            serverUrl = "http://" + serverUrl;
        }
        if (serverUrl.endsWith("/")) {
            serverUrl = serverUrl.substring(0, serverUrl.length() - 1);
        }

        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        SharedPreferences.Editor editor = prefs.edit();

        editor.putString("server_url", serverUrl);
        editor.putString("username", username);
        editor.putString("password", password);

        boolean isLan = radioPrinterType.getCheckedRadioButtonId() == R.id.radioLan;
        editor.putString("printer_type", isLan ? "lan" : "bluetooth");

        // Save paper width
        int paperWidthSelection = spinnerPaperWidth.getSelectedItemPosition();
        int paperWidth;
        switch (paperWidthSelection) {
            case 1: paperWidth = 60; break;
            case 2: paperWidth = 40; break;
            default: paperWidth = 80; break;
        }
        editor.putInt("paper_width", paperWidth);

        if (isLan) {
            String lanAddress = editLanAddress.getText().toString().trim();
            if (lanAddress.isEmpty()) {
                showToast("Alamat IP Printer LAN tidak boleh kosong");
                return;
            }
            editor.putString("lan_printer_address", lanAddress);
            editor.remove("printer_address");
            editor.remove("printer_name");
        } else {
            saveBluetoothPrinterSelection(editor);
        }

        editor.apply();

        // Reload WebView if URL changed
        String currentUrl = getServerUrl();
        if (!currentUrl.equals(serverUrl)) {
            webView.loadUrl(serverUrl);
        }

        showToast("Pengaturan disimpan");
        settingsPanel.setVisibility(View.GONE);
        settingsVisible = false;

        // Restart service if running
        if (isServiceRunning.get()) {
            restartPrintService();
        }
    }

    private void saveBluetoothPrinterSelection(SharedPreferences.Editor editor) {
        if (spinnerPrinter.getSelectedItem() != null) {
            String selectedPrinter = spinnerPrinter.getSelectedItem().toString();

            if (!selectedPrinter.startsWith("Tidak ada") &&
                    !selectedPrinter.startsWith("Bluetooth") &&
                    !selectedPrinter.startsWith("Izin") &&
                    selectedPrinter.contains("(")) {

                int parenStart = selectedPrinter.lastIndexOf('(');
                int parenEnd = selectedPrinter.lastIndexOf(')');

                if (parenStart >= 0 && parenEnd > parenStart) {
                    String printerAddress = selectedPrinter.substring(parenStart + 1, parenEnd);
                    String printerName = selectedPrinter.substring(0, parenStart).trim();

                    editor.putString("printer_name", printerName);
                    editor.putString("printer_address", printerAddress);
                    editor.remove("lan_printer_address");
                }
            }
        }
    }

    private void restartPrintService() {
        backgroundExecutor.execute(() -> {
            // Stop service
            Intent stopIntent = new Intent(MainActivity.this, PrintService.class);
            stopService(stopIntent);

            // Wait a bit
            try {
                Thread.sleep(1000);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }

            // Start again
            Intent startIntent = new Intent(MainActivity.this, PrintService.class);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(startIntent);
            } else {
                startService(startIntent);
            }
        });
    }

    // Bluetooth Printer Management
    private void loadPairedPrinters() {
        backgroundExecutor.execute(() -> {
            List<String> printerList = new ArrayList<>();
            BluetoothAdapter bluetoothAdapter = BluetoothAdapter.getDefaultAdapter();
            int selectedIndex = 0;

            if (bluetoothAdapter == null) {
                printerList.add("❌ Bluetooth tidak tersedia");
            } else if (!bluetoothAdapter.isEnabled()) {
                printerList.add("⚠️ Bluetooth mati, nyalakan dulu");
            } else {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                            != PackageManager.PERMISSION_GRANTED) {
                        printerList.add("⚠️ Izin Bluetooth diperlukan");
                        updateSpinnerItems(printerList, 0);
                        return;
                    }
                }

                Set<BluetoothDevice> pairedDevices = bluetoothAdapter.getBondedDevices();

                if (pairedDevices.isEmpty()) {
                    printerList.add("📭 Tidak ada printer terpasang");
                } else {
                    String savedAddress = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
                            .getString("printer_address", "");
                    int i = 0;

                    for (BluetoothDevice device : pairedDevices) {
                        String name = device.getName() != null ? device.getName() : "Unknown";
                        String entry = "🖨️ " + name + " (" + device.getAddress() + ")";
                        printerList.add(entry);

                        if (device.getAddress().equals(savedAddress)) {
                            selectedIndex = i;
                        }
                        i++;
                    }
                }
            }

            updateSpinnerItems(printerList, selectedIndex);
        });
    }

    private void updateSpinnerItems(List<String> items, int selection) {
        runOnUiThread(() -> {
            ArrayAdapter<String> adapter = new ArrayAdapter<>(this,
                    android.R.layout.simple_spinner_item, items);
            adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
            spinnerPrinter.setAdapter(adapter);

            if (selection < items.size()) {
                spinnerPrinter.setSelection(selection);
            }
        });
    }

    // Print Service Control
    private void togglePrintService() {
        if (isServiceRunning.get()) {
            stopPrintService();
        } else {
            startPrintService();
        }
    }

    private void startPrintService() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String serverUrl = prefs.getString("server_url", "");
        String printerType = prefs.getString("printer_type", "bluetooth");
        String printerAddress = prefs.getString("printer_address", "");
        String lanAddress = prefs.getString("lan_printer_address", "");
        String username = prefs.getString("username", "");
        String password = prefs.getString("password", "");

        // Validasi
        if (serverUrl.isEmpty()) {
            showToast("Atur URL server terlebih dahulu");
            toggleSettings();
            return;
        }

        if (username.isEmpty() || password.isEmpty()) {
            showToast("Username dan password harus diisi");
            toggleSettings();
            return;
        }

        if ("lan".equals(printerType)) {
            if (lanAddress.isEmpty()) {
                showToast("Atur IP printer LAN terlebih dahulu");
                toggleSettings();
                return;
            }
        } else {
            if (printerAddress.isEmpty()) {
                showToast("Pilih printer Bluetooth terlebih dahulu");
                toggleSettings();
                return;
            }
        }

        // Simpan status running di preferences
        prefs.edit().putBoolean("service_running", true).apply();
        isServiceRunning.set(true);

        // Start service
        Intent intent = new Intent(this, PrintService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }

        updateServiceStatus(true, 0, 0, "Starting...");
        showToast("Print Service dimulai");
    }

    private void stopPrintService() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        prefs.edit().putBoolean("service_running", false).apply();
        isServiceRunning.set(false);

        Intent intent = new Intent(this, PrintService.class);
        stopService(intent);

        updateServiceStatus(false, 0, 0, "Stopped");
        showToast("Print Service dihentikan");
    }

    private void updateServiceStatusFromPrefs() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        boolean running = prefs.getBoolean("service_running", false);
        int printed = prefs.getInt("printed_count", 0);
        int failed = prefs.getInt("failed_count", 0);

        isServiceRunning.set(running);
        updateServiceStatus(running, printed, failed, null);
    }

    private void updateServiceStatus(boolean isRunning, int printed, int failed, String status) {
        runOnUiThread(() -> {
            if (isRunning) {
                txtServiceStatus.setText("🟢 PRINT SERVICE AKTIF");
                txtServiceStatus.setTextColor(ContextCompat.getColor(this, android.R.color.holo_green_light));
                btnToggleService.setText("⏹ STOP");
                btnToggleService.setBackgroundTintList(android.content.res.ColorStateList.valueOf(android.graphics.Color.parseColor("#dc2626")));

                if (txtPrintStats != null) {
                    txtPrintStats.setVisibility(View.GONE);
                }
            } else {
                txtServiceStatus.setText("🔴 PRINT SERVICE MATI");
                txtServiceStatus.setTextColor(ContextCompat.getColor(this, android.R.color.holo_red_light));
                btnToggleService.setText("▶ START");
                btnToggleService.setBackgroundTintList(android.content.res.ColorStateList.valueOf(android.graphics.Color.parseColor("#059669")));
                if (txtPrintStats != null) {
                    txtPrintStats.setVisibility(View.GONE);
                }
            }
        });
    }

    // Validations
    private void validatePrinterSelection() {
        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
        String printerAddress = prefs.getString("printer_address", "");

        if (printerAddress.isEmpty()) {
            showToast("Pilih printer Bluetooth");
        }
    }

    private void validateLanAddress() {
        String lanAddress = editLanAddress.getText().toString().trim();
        if (lanAddress.isEmpty()) {
            showToast("Masukkan alamat IP printer LAN");
        }
    }

    // Permissions
    private void requestAppPermissions() {
        List<String> needed = new ArrayList<>();

        // Bluetooth permissions
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT)
                    != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.BLUETOOTH_CONNECT);
            }
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_SCAN)
                    != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.BLUETOOTH_SCAN);
            }
        }

        // Notifications
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.POST_NOTIFICATIONS);
            }
        }

        // Camera
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            needed.add(Manifest.permission.CAMERA);
        }

        // Location
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED) {
            needed.add(Manifest.permission.ACCESS_FINE_LOCATION);
        }

        // Storage (for older Android)
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_EXTERNAL_STORAGE)
                    != PackageManager.PERMISSION_GRANTED) {
                needed.add(Manifest.permission.READ_EXTERNAL_STORAGE);
            }
        }

        if (!needed.isEmpty()) {
            permissionLauncher.launch(needed.toArray(new String[0]));
        }
    }

    private void showPermissionDeniedDialog() {
        new AlertDialog.Builder(this)
                .setTitle("Izin Diperlukan")
                .setMessage("Aplikasi membutuhkan beberapa izin untuk berfungsi dengan baik. " +
                        "Silakan berikan izin di Pengaturan.")
                .setPositiveButton("Buka Pengaturan", (d, w) -> {
                    Intent intent = new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
                    intent.setData(Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                })
                .setNegativeButton("Tutup", null)
                .show();
    }

    // Helpers
    private String getServerUrl() {
        return getSharedPreferences(PREFS_NAME, MODE_PRIVATE).getString("server_url", DEFAULT_SERVER_URL);
    }

    private void showToast(String message) {
        runOnUiThread(() -> Toast.makeText(this, message, Toast.LENGTH_SHORT).show());
    }

    // Lifecycle
    @Override
    protected void onResume() {
        super.onResume();
        updateServiceStatusFromPrefs();
    }

    @Override
    protected void onPause() {
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        Log.i(TAG, "MainActivity destroying");

        // Unregister receiver
        try {
            LocalBroadcastManager.getInstance(this).unregisterReceiver(serviceStatusReceiver);
        } catch (Exception e) {
            Log.e(TAG, "Error unregistering receiver: " + e.getMessage());
        }

        // Unregister network callback
        if (connectivityManager != null && networkCallback != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (Exception e) {
                Log.e(TAG, "Error unregistering network callback: " + e.getMessage());
            }
        }

        // Remove callbacks (termasuk auto-retry dan health check)
        mainHandler.removeCallbacksAndMessages(null);

        // Shutdown executor
        if (backgroundExecutor != null && !backgroundExecutor.isShutdown()) {
            backgroundExecutor.shutdown();
        }

        // Clean up WebView
        if (webView != null) {
            webView.loadUrl("about:blank");
            webView.destroy();
        }

        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (settingsVisible) {
            settingsPanel.setVisibility(View.GONE);
            settingsVisible = false;
        } else if (webView.canGoBack()) {
            webView.goBack();
        } else {
            new AlertDialog.Builder(this)
                    .setTitle("Keluar Aplikasi?")
                    .setMessage("Print Service akan tetap berjalan di latar belakang.")
                    .setPositiveButton("Keluar", (d, w) -> {
                        super.onBackPressed(); // Panggil super.onBackPressed()
                    })
                    .setNegativeButton("Batal", null)
                    .show();
        }
    }
}