package com.dapoerterasobor.printservice;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothSocket;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.io.OutputStream;
import java.net.SocketTimeoutException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import io.socket.client.IO;
import io.socket.client.Socket;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

public class PrintService extends Service {

    private static final String TAG = "PrintService";
    private static final String CHANNEL_ID = "print_service_channel";
    private static final int NOTIFICATION_ID = 1001;
    private static final String PREFS_NAME = "PrintServicePrefs";
    private static final UUID SPP_UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB");

    // Timeouts
    private static final int SOCKET_TIMEOUT = 10000;
    private static final int PRINTER_CONNECT_TIMEOUT = 8000;
    private static final int SHUTDOWN_TIMEOUT = 3000;
    private static final int PERIODIC_CHECK_INTERVAL = 15000; // 15 detik (lebih cepat)
    private static final int PRINT_CHECK_DELAY = 1000; // 1 detik setelah connect
    private static final int MAX_RETRY_COUNT = 3;
    private static final int RETRY_DELAY_MS = 5000; // 5 detik antar retry

    private Socket socket;
    private BluetoothSocket bluetoothSocket;
    private java.net.Socket lanSocket;
    private OutputStream printerOutputStream;
    private String printerAddress;
    private String printerType;
    private String serverUrl;
    private String authToken;

    private Handler mainHandler;
    private Handler periodicHandler;
    private Runnable periodicCheckRunnable;
    private ExecutorService printExecutor;
    private ExecutorService networkExecutor;

    // Printer status dengan flag yang lebih eksplisit
    private volatile boolean isPrinterConnected = false;
    private volatile boolean isPrinterConnecting = false;
    private volatile boolean hasCheckedPendingAfterConnect = false;
    private volatile long lastPrinterConnectionTime = 0;

    private final AtomicBoolean isServiceRunning = new AtomicBoolean(false);
    private final AtomicBoolean isReconnecting = new AtomicBoolean(false);
    private final AtomicInteger printedCount = new AtomicInteger(0);
    private final AtomicInteger failedCount = new AtomicInteger(0);

    // Untuk tracking print jobs dan retry
    private final ConcurrentHashMap<Integer, Future<?>> printJobs = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<Integer, RetryJobInfo> retryJobs = new ConcurrentHashMap<>();
    private final ConcurrentLinkedQueue<JSONObject> pendingPrintQueue = new ConcurrentLinkedQueue<>();

    // Lock objects untuk sinkronisasi
    private final Object printerLock = new Object();
    private final Object socketLock = new Object();
    private final Object printQueueLock = new Object();

    // Kelas untuk menyimpan info job yang akan di-retry
    private static class RetryJobInfo {
        JSONObject jobData;
        int retryCount;
        long lastRetryTime;

        RetryJobInfo(JSONObject jobData) {
            this.jobData = jobData;
            this.retryCount = 0;
            this.lastRetryTime = 0;
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        Log.i(TAG, "PrintService created");

        mainHandler = new Handler(Looper.getMainLooper());
        periodicHandler = new Handler(Looper.getMainLooper());

        printExecutor = Executors.newFixedThreadPool(3);
        networkExecutor = Executors.newSingleThreadExecutor();

        isServiceRunning.set(true);
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Log.i(TAG, "PrintService starting...");
        startForeground(NOTIFICATION_ID, buildNotification("Memulai service..."));

        // Reset flag setiap kali service start
        hasCheckedPendingAfterConnect = false;
        lastPrinterConnectionTime = 0;

        networkExecutor.execute(() -> {
            if (!isServiceRunning.get()) return;

            try {
                SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
                String newServerUrl = prefs.getString("server_url", "");
                String newPrinterAddress = prefs.getString("printer_address", "");
                String newPrinterType = prefs.getString("printer_type", "bluetooth");
                String lanAddress = prefs.getString("lan_printer_address", "");
                String username = prefs.getString("username", "");
                String password = prefs.getString("password", "");

                if (newServerUrl.isEmpty()) {
                    updateNotification("❌ Server URL belum diisi");
                    return;
                }

                if (username.isEmpty() || password.isEmpty()) {
                    updateNotification("❌ Username/password belum diisi");
                    return;
                }

                serverUrl = newServerUrl;
                printerAddress = !"lan".equals(newPrinterType) ? newPrinterAddress : lanAddress;
                printerType = newPrinterType;

                if (!testServerConnection(serverUrl)) {
                    updateNotification("❌ Server tidak dapat dijangkau");
                    return;
                }

                updateNotification("⏳ Mengautentikasi ke server...");
                authToken = authenticate(serverUrl, username, password);

                if (authToken == null) {
                    Log.e(TAG, "Authentication failed, stopping service startup");
                    updateNotification("❌ Autentikasi gagal. Cek username/password");
                    return;
                }

                // Connect printer (akan trigger pengecekan pending print)
                if (!printerAddress.isEmpty()) {
                    updateNotification("🔌 Menghubungkan printer...");
                    connectPrinter();
                } else {
                    updateNotification("⚠️ Printer belum dikonfigurasi");
                }

                updateNotification("🌐 Menghubungkan WebSocket...");
                connectWebSocket();

                // Periodic check untuk jaga-jaga
                startPeriodicCheck();

            } catch (Exception e) {
                Log.e(TAG, "Error in startup: " + e.getMessage());
                updateNotification("Error: " + e.getMessage());
            }
        });

        return START_STICKY;
    }

    private void startPeriodicCheck() {
        if (periodicCheckRunnable != null) {
            periodicHandler.removeCallbacks(periodicCheckRunnable);
        }

        periodicCheckRunnable = new Runnable() {
            @Override
            public void run() {
                if (isServiceRunning.get()) {
                    // Cek printer connection
                    if (!isPrinterConnected && !printerAddress.isEmpty() && !isPrinterConnecting) {
                        Log.d(TAG, "Periodic check: printer disconnected, attempting reconnect...");
                        connectPrinter();
                    }

                    // Cek pending prints jika printer connected
                    if (isPrinterConnected && authToken != null) {
                        Log.d(TAG, "Periodic check for pending prints...");
                        fetchPendingPrints();
                    }

                    // Proses retry jobs
                    processRetryJobs();
                }
                if (periodicHandler != null && isServiceRunning.get()) {
                    periodicHandler.postDelayed(this, PERIODIC_CHECK_INTERVAL);
                }
            }
        };

        periodicHandler.postDelayed(periodicCheckRunnable, PERIODIC_CHECK_INTERVAL);
        Log.i(TAG, "Periodic check started every " + (PERIODIC_CHECK_INTERVAL/1000) + " seconds");
    }

    private boolean testServerConnection(String url) {
        OkHttpClient client = new OkHttpClient.Builder()
                .connectTimeout(5000, TimeUnit.MILLISECONDS)
                .build();

        Request request = new Request.Builder()
                .url(url + "/")
                .head()
                .build();

        try (Response response = client.newCall(request).execute()) {
            Log.d(TAG, "Server test: " + response.code());
            return response.isSuccessful() || response.code() == 200;
        } catch (Exception e) {
            Log.e(TAG, "Server test failed: " + e.getMessage());
            return false;
        }
    }

    private String authenticate(String serverUrl, String username, String password) {
        OkHttpClient client = new OkHttpClient.Builder()
                .connectTimeout(SOCKET_TIMEOUT, TimeUnit.MILLISECONDS)
                .readTimeout(SOCKET_TIMEOUT, TimeUnit.MILLISECONDS)
                .writeTimeout(SOCKET_TIMEOUT, TimeUnit.MILLISECONDS)
                .build();

        if (serverUrl == null || serverUrl.isEmpty()) {
            Log.e(TAG, "Server URL is empty");
            updateNotification("Server URL belum diisi");
            return null;
        }

        if (username == null || username.isEmpty() || password == null || password.isEmpty()) {
            Log.e(TAG, "Username or password is empty");
            updateNotification("Username/password belum diisi");
            return null;
        }

        try {
            JSONObject json = new JSONObject();
            json.put("username", username);
            json.put("password", password);
            json.put("device_name", Build.MODEL + " - " + Build.VERSION.RELEASE);

            // Request role printer_operator jika ada
            json.put("role", "printer_operator");

            Log.d(TAG, "Authenticating to: " + serverUrl + "/api/auth/token");

            RequestBody body = RequestBody.create(
                    json.toString(),
                    MediaType.parse("application/json; charset=utf-8")
            );

            Request request = new Request.Builder()
                    .url(serverUrl + "/api/auth/token")
                    .post(body)
                    .build();

            try (Response response = client.newCall(request).execute()) {
                int statusCode = response.code();
                Log.d(TAG, "Auth response code: " + statusCode);

                if (response.isSuccessful() && response.body() != null) {
                    String responseBody = response.body().string();
                    JSONObject result = new JSONObject(responseBody);

                    if (result.optBoolean("success", false)) {
                        String token = result.getString("token");
                        Log.i(TAG, "✅ Authentication successful");
                        return token;
                    } else {
                        String errorMsg = result.optString("error", "Unknown error");
                        Log.e(TAG, "❌ Auth failed: " + errorMsg);
                        updateNotification("Autentikasi gagal: " + errorMsg);
                    }
                } else {
                    Log.e(TAG, "❌ HTTP error " + statusCode);
                    switch (statusCode) {
                        case 401:
                            updateNotification("Username/password salah");
                            break;
                        case 403:
                            updateNotification("Akun tidak aktif atau role tidak sesuai");
                            break;
                        case 404:
                            updateNotification("Server tidak ditemukan");
                            break;
                        default:
                            updateNotification("Error HTTP " + statusCode);
                    }
                }
            }
        } catch (SocketTimeoutException e) {
            Log.e(TAG, "⏱️ Auth timeout");
            updateNotification("Timeout - server tidak merespons");
        } catch (IOException e) {
            Log.e(TAG, "🔌 Connection error: " + e.getMessage());
            updateNotification("Tidak bisa terhubung ke server");
        } catch (Exception e) {
            Log.e(TAG, "💥 Auth error: " + e.getMessage());
            updateNotification("Error: " + e.getMessage());
        }

        return null;
    }

    private void connectPrinter() {
        if (printerAddress == null || printerAddress.isEmpty()) {
            return;
        }

        // Hindari multiple connection attempts
        if (isPrinterConnecting) {
            Log.d(TAG, "Printer connection already in progress");
            return;
        }

        isPrinterConnecting = true;

        printExecutor.execute(() -> {
            synchronized (printerLock) {
                try {
                    if ("lan".equals(printerType)) {
                        connectLanPrinter();
                    } else {
                        connectBluetoothPrinter();
                    }
                } finally {
                    isPrinterConnecting = false;
                }
            }
        });
    }

    private void connectLanPrinter() {
        try {
            String host;
            int port = 9100;

            if (printerAddress.contains(":")) {
                String[] parts = printerAddress.split(":");
                host = parts[0];
                port = Integer.parseInt(parts[1]);
            } else {
                host = printerAddress;
            }

            closePrinterConnection();

            lanSocket = new java.net.Socket();
            lanSocket.connect(new java.net.InetSocketAddress(host, port), PRINTER_CONNECT_TIMEOUT);
            lanSocket.setSoTimeout(SOCKET_TIMEOUT);
            printerOutputStream = lanSocket.getOutputStream();

            // Set status connected
            boolean wasConnected = isPrinterConnected;
            isPrinterConnected = true;
            isReconnecting.set(false);
            lastPrinterConnectionTime = System.currentTimeMillis();

            Log.i(TAG, "✅ LAN Printer connected: " + printerAddress);
            updateNotification("✅ LAN Printer: " + printerAddress);

            // Trigger pengecekan pending print
            onPrinterConnected(wasConnected);

        } catch (IOException e) {
            Log.e(TAG, "❌ LAN printer connection failed: " + e.getMessage());
            updateNotification("❌ Gagal terhubung ke LAN printer");
            isPrinterConnected = false;
            scheduleReconnect();
        }
    }

    private void connectBluetoothPrinter() {
        try {
            BluetoothAdapter adapter = BluetoothAdapter.getDefaultAdapter();
            if (adapter == null) {
                Log.e(TAG, "Bluetooth not available");
                updateNotification("❌ Bluetooth tidak tersedia");
                isPrinterConnecting = false;
                return;
            }

            if (!adapter.isEnabled()) {
                Log.e(TAG, "Bluetooth is disabled");
                updateNotification("❌ Bluetooth dimatikan");
                isPrinterConnecting = false;
                return;
            }

            BluetoothDevice device = adapter.getRemoteDevice(printerAddress);
            closePrinterConnection();

            bluetoothSocket = device.createRfcommSocketToServiceRecord(SPP_UUID);
            adapter.cancelDiscovery();

            Future<?> connectFuture = printExecutor.submit(() -> {
                try {
                    bluetoothSocket.connect();
                } catch (IOException e) {
                    Log.e(TAG, "Bluetooth connect error", e);
                }
            });

            try {
                connectFuture.get(PRINTER_CONNECT_TIMEOUT, TimeUnit.MILLISECONDS);
                printerOutputStream = bluetoothSocket.getOutputStream();

                // Set status connected
                boolean wasConnected = isPrinterConnected;
                isPrinterConnected = true;
                isReconnecting.set(false);
                lastPrinterConnectionTime = System.currentTimeMillis();

                Log.i(TAG, "✅ Printer connected: " + device.getName());
                updateNotification("✅ Terhubung ke " + device.getName());

                // Trigger pengecekan pending print
                onPrinterConnected(wasConnected);

            } catch (Exception e) {
                connectFuture.cancel(true);
                try {
                    if (bluetoothSocket != null) {
                        bluetoothSocket.close();
                    }
                } catch (IOException ignored) {}
                throw new IOException("Connection timeout", e);
            }

        } catch (SecurityException e) {
            Log.e(TAG, "Bluetooth permission error");
            updateNotification("❌ Izin Bluetooth diperlukan");
        } catch (Exception e) {
            Log.e(TAG, "❌ Printer connection failed: " + e.getMessage());
            updateNotification("❌ Gagal terhubung ke printer");
            isPrinterConnected = false;
            scheduleReconnect();
        }
    }

    private void onPrinterConnected(boolean wasConnected) {
        // Selalu reset flag agar pending print selalu dicek saat printer terhubung
        hasCheckedPendingAfterConnect = false;
        Log.i(TAG, "Printer connected (wasConnected=" + wasConnected + "), resetting check flag");

        // Selalu cek pending print saat printer terhubung
        Log.i(TAG, "🔍 Printer connected, scheduling pending print check with retry...");

        // Proses job yang tertunda di local queue terlebih dahulu
        processQueuedPrintJobs();

        // Delay singkat lalu fetch pending prints dari server
        mainHandler.postDelayed(() -> {
            if (isPrinterConnected && !hasCheckedPendingAfterConnect) {
                Log.i(TAG, "🔍 Checking for pending prints after printer connection");
                fetchPendingPrintsWithRetry(MAX_RETRY_COUNT);
            }
        }, PRINT_CHECK_DELAY);

        // Tambahan: cek lagi setelah 5 detik untuk memastikan semua pending terproses
        mainHandler.postDelayed(() -> {
            if (isPrinterConnected && isServiceRunning.get()) {
                Log.i(TAG, "🔍 Secondary pending print check after printer connection");
                fetchPendingPrints();
                processQueuedPrintJobs();
            }
        }, 5000);
    }

    private void fetchPendingPrintsWithRetry(int maxRetries) {
        new Thread(() -> {
            int retry = 0;
            boolean success = false;

            while (retry < maxRetries && isServiceRunning.get() && !success) {
                if (isPrinterConnected) {
                    success = fetchPendingPrints();
                    if (success) {
                        hasCheckedPendingAfterConnect = true;
                        Log.i(TAG, "✅ Successfully fetched pending prints after " + (retry + 1) + " attempt(s)");
                        break;
                    }
                }

                retry++;
                if (retry < maxRetries) {
                    Log.w(TAG, "Retry " + retry + "/" + maxRetries + " to fetch pending prints in " + RETRY_DELAY_MS + "ms");
                    try { Thread.sleep(RETRY_DELAY_MS); } catch (InterruptedException e) {}
                }
            }

            if (!success && retry >= maxRetries) {
                Log.w(TAG, "Failed to fetch pending prints after " + maxRetries + " attempts");
                hasCheckedPendingAfterConnect = false; // Reset flag untuk coba lagi nanti
            }
        }).start();
    }

    private boolean fetchPendingPrints() {
        if (serverUrl == null || authToken == null) {
            Log.e(TAG, "Cannot fetch pending prints: No server URL or token");
            return false;
        }

        // Hanya fetch jika printer connected
        if (!isPrinterConnected) {
            Log.d(TAG, "Printer not connected, skipping pending print fetch");
            return false;
        }

        final boolean[] success = {false};

        try {
            OkHttpClient client = new OkHttpClient.Builder()
                    .connectTimeout(SOCKET_TIMEOUT, TimeUnit.MILLISECONDS)
                    .readTimeout(SOCKET_TIMEOUT, TimeUnit.MILLISECONDS)
                    .build();

            String url = serverUrl + "/api/pending-prints/unprocessed?token=" + authToken;

            Log.d(TAG, "Fetching pending prints...");

            Request request = new Request.Builder()
                    .url(url)
                    .get()
                    .build();

            try (Response response = client.newCall(request).execute()) {
                if (response.isSuccessful() && response.body() != null) {
                    String responseBody = response.body().string();
                    JSONObject result = new JSONObject(responseBody);

                    if (result.optBoolean("success", false)) {
                        JSONArray prints = result.optJSONArray("pending_prints");
                        int count = result.optInt("count", 0);

                        if (count > 0) {
                            Log.i(TAG, "📋 Found " + count + " pending prints");
                            updateNotification("📋 Memproses " + count + " antrian print...");

                            for (int i = 0; i < prints.length(); i++) {
                                JSONObject printJob = prints.getJSONObject(i);
                                addToPrintQueue(printJob);
                            }
                        } else {
                            Log.d(TAG, "No pending prints found");
                        }
                        success[0] = true;
                    }
                }
            }
        } catch (Exception e) {
            Log.e(TAG, "Error fetching pending prints: " + e.getMessage());
            success[0] = false;
        }

        return success[0];
    }

    private void addToPrintQueue(JSONObject printJob) {
        synchronized (printQueueLock) {
            pendingPrintQueue.offer(printJob);
        }

        // Coba proses langsung jika printer connected
        if (isPrinterConnected) {
            processQueuedPrintJobs();
        } else {
            Log.d(TAG, "Printer not connected, job #" + printJob.optInt("id") + " queued for later");
        }
    }

    private void processQueuedPrintJobs() {
        if (!isPrinterConnected) {
            Log.d(TAG, "Printer not connected, cannot process queued jobs");
            return;
        }

        synchronized (printQueueLock) {
            while (!pendingPrintQueue.isEmpty()) {
                JSONObject printJob = pendingPrintQueue.poll();
                if (printJob != null) {
                    Future<?> jobFuture = printExecutor.submit(() -> processPrintJob(printJob));
                    printJobs.put(printJob.optInt("id"), jobFuture);
                }
            }
        }
    }

    private void processRetryJobs() {
        if (!isPrinterConnected) {
            return;
        }

        long now = System.currentTimeMillis();
        for (RetryJobInfo retryInfo : retryJobs.values()) {
            if (retryInfo.retryCount < MAX_RETRY_COUNT &&
                    (now - retryInfo.lastRetryTime) > RETRY_DELAY_MS) {

                retryInfo.lastRetryTime = now;
                retryInfo.retryCount++;

                Log.i(TAG, "Retrying job #" + retryInfo.jobData.optInt("id") +
                        " attempt " + retryInfo.retryCount + "/" + MAX_RETRY_COUNT);

                Future<?> jobFuture = printExecutor.submit(() -> {
                    boolean success = processPrintJobInternal(retryInfo.jobData);
                    if (success) {
                        retryJobs.remove(retryInfo.jobData.optInt("id"));
                    }
                });
                printJobs.put(retryInfo.jobData.optInt("id"), jobFuture);
            }
        }
    }

    private void connectWebSocket() {
        if (authToken == null) {
            Log.e(TAG, "Cannot connect WebSocket: No auth token");
            return;
        }

        if (serverUrl == null || serverUrl.isEmpty()) {
            Log.e(TAG, "Server URL is empty");
            return;
        }

        networkExecutor.execute(() -> {
            synchronized (socketLock) {
                try {
                    IO.Options options = new IO.Options();
                    options.transports = new String[]{"websocket"};
                    options.reconnection = true;
                    options.reconnectionAttempts = Integer.MAX_VALUE;
                    options.reconnectionDelay = 2000;
                    options.reconnectionDelayMax = 10000;
                    options.timeout = SOCKET_TIMEOUT;
                    options.query = "";

                    if (socket == null) {
                        socket = IO.socket(URI.create(serverUrl), options);

                        socket.on(Socket.EVENT_CONNECT, args -> {
                            Log.i(TAG, "✅ WebSocket connected");
                            updateNotification("✅ Online - menunggu pesanan");

                            try {
                                JSONObject joinData = new JSONObject();
                                joinData.put("branch_id", "all");
                                socket.emit("join_branch", joinData);
                            } catch (Exception e) {
                                Log.e(TAG, "Error joining branch: " + e.getMessage());
                            }

                            // Cek pending print saat WebSocket connect
                            if (isPrinterConnected) {
                                fetchPendingPrintsWithRetry(MAX_RETRY_COUNT);
                            }
                        });

                        socket.on(Socket.EVENT_CONNECT_ERROR, args -> {
                            String error = args.length > 0 ? args[0].toString() : "Unknown error";
                            Log.e(TAG, "❌ WebSocket error: " + error);
                            updateNotification("❌ Koneksi WebSocket gagal");
                        });

                        socket.on(Socket.EVENT_DISCONNECT, args -> {
                            Log.w(TAG, "WebSocket disconnected");
                            updateNotification("⚠️ Terputus, mencoba reconnect...");
                        });

                        socket.on("new_print_job", args -> {
                            try {
                                JSONObject printJob = (JSONObject) args[0];
                                Log.i(TAG, "📨 New print job: #" + printJob.optInt("id"));
                                updateNotification("🖨️ Mencetak struk #" + printJob.optInt("id"));

                                addToPrintQueue(printJob);
                            } catch (Exception e) {
                                Log.e(TAG, "Error processing print job: " + e.getMessage());
                            }
                        });
                    }

                    if (!socket.connected()) {
                        socket.connect();
                    }

                } catch (Exception e) {
                    Log.e(TAG, "WebSocket setup error: " + e.getMessage());
                }
            }
        });
    }

    private void processPrintJob(JSONObject printJob) {
        boolean success = processPrintJobInternal(printJob);

        if (!success) {
            // Simpan untuk retry nanti
            int printId = printJob.optInt("id");
            if (!retryJobs.containsKey(printId)) {
                retryJobs.put(printId, new RetryJobInfo(printJob));
            }
        }
    }

    private boolean processPrintJobInternal(JSONObject printJob) {
        int printId = -1;
        try {
            printId = printJob.getInt("id");
            String receiptDataStr = printJob.getString("receipt_data");
            JSONArray receiptData = new JSONArray(receiptDataStr);

            // CEK PRINTER STATUS
            if (!isPrinterConnected) {
                Log.w(TAG, "Printer not connected for job #" + printId);
                return false;
            }

            // Kirim acknowledgment ke server
            sendPrintJobReceived(printId);

            byte[] printData = convertReceiptData(receiptData);

            synchronized (printerOutputStream) {
                printerOutputStream.write(printData);
                printerOutputStream.flush();
            }

            printedCount.incrementAndGet();
            reportPrintSuccess(printId);

            updateNotification(String.format("✅ Online | Dicetak: %d", printedCount.get()));

            Log.i(TAG, "✅ Print job #" + printId + " completed");
            return true;

        } catch (Exception e) {
            Log.e(TAG, "❌ Print error: " + e.getMessage());
            if (printId >= 0) {
                failedCount.incrementAndGet();
                reportPrintFailure(printId, e.getMessage());
            }
            return false;
        } finally {
            if (printId >= 0) {
                printJobs.remove(printId);
            }
        }
    }

    private void sendPrintJobReceived(int printId) {
        if (socket != null && socket.connected()) {
            try {
                JSONObject data = new JSONObject();
                data.put("print_id", printId);
                socket.emit("print_job_received", data);
            } catch (Exception e) {
                Log.e(TAG, "Error sending receipt via WebSocket");
            }
        }
    }

    private byte[] convertReceiptData(JSONArray data) throws Exception {
        if (data == null || data.length() == 0) {
            return new byte[0];
        }

        Object first = data.opt(0);
        if (first instanceof Integer) {
            byte[] bytes = new byte[data.length()];
            for (int i = 0; i < data.length(); i++) {
                bytes[i] = (byte) data.getInt(i);
            }
            return bytes;
        } else {
            // Deteksi paper width dari settings
            // 80mm = 48 chars, 60mm = 35 chars, 40mm = 24 chars
            SharedPreferences prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE);
            int paperWidth = prefs.getInt("paper_width", 80);
            int charWidth;
            switch (paperWidth) {
                case 40: charWidth = 24; break;
                case 60: charWidth = 35; break;
                default: charWidth = 48; break;  // 80mm default
            }

            StringBuilder sb = new StringBuilder();
            byte[] initCmd = {0x1B, 0x40};
            byte[] cutCmd = {0x1D, 0x56, 0x00};
            byte[] feedCmd = {0x1B, 0x64, 0x04};

            // ESC/POS alignment commands
            byte[] alignLeft = {0x1B, 0x61, 0x00};
            byte[] alignCenter = {0x1B, 0x61, 0x01};
            byte[] alignRight = {0x1B, 0x61, 0x02};
            byte[] boldOn = {0x1B, 0x45, 0x01};
            byte[] boldOff = {0x1B, 0x45, 0x00};
            byte[] doubleHeight = {0x1B, 0x21, 0x10};
            byte[] normalSize = {0x1B, 0x21, 0x00};

            sb.append(new String(initCmd, StandardCharsets.ISO_8859_1));

            for (int i = 0; i < data.length(); i++) {
                JSONObject cmd = data.optJSONObject(i);
                if (cmd == null) continue;

                String type = cmd.optString("type", "text");
                String value = cmd.optString("value", "");
                String align = cmd.optString("align", "");
                boolean bold = cmd.optBoolean("bold", false);
                String size = cmd.optString("size", "");

                switch (type) {
                    case "text":
                        // Sesuaikan separator dengan paper width
                        if (isAllSameChar(value, '=')) {
                            value = repeatChar('=', charWidth);
                        } else if (isAllSameChar(value, '-')) {
                            value = repeatChar('-', charWidth);
                        }
                        // Handle alignment
                        if ("center".equals(align)) {
                            sb.append(new String(alignCenter, StandardCharsets.ISO_8859_1));
                        } else if ("right".equals(align)) {
                            sb.append(new String(alignRight, StandardCharsets.ISO_8859_1));
                        }
                        // Handle bold
                        if (bold) {
                            sb.append(new String(boldOn, StandardCharsets.ISO_8859_1));
                        }
                        // Handle size
                        if ("large".equals(size)) {
                            sb.append(new String(doubleHeight, StandardCharsets.ISO_8859_1));
                        }
                        sb.append(value).append("\n");
                        // Reset formatting
                        if ("large".equals(size)) {
                            sb.append(new String(normalSize, StandardCharsets.ISO_8859_1));
                        }
                        if (bold) {
                            sb.append(new String(boldOff, StandardCharsets.ISO_8859_1));
                        }
                        if ("center".equals(align) || "right".equals(align)) {
                            sb.append(new String(alignLeft, StandardCharsets.ISO_8859_1));
                        }
                        break;
                    case "bold_text":
                        sb.append(new String(boldOn, StandardCharsets.ISO_8859_1));
                        sb.append(value).append("\n");
                        sb.append(new String(boldOff, StandardCharsets.ISO_8859_1));
                        break;
                    case "center":
                        sb.append(new String(alignCenter, StandardCharsets.ISO_8859_1));
                        sb.append(value).append("\n");
                        sb.append(new String(alignLeft, StandardCharsets.ISO_8859_1));
                        break;
                    case "separator":
                        sb.append(repeatChar('=', charWidth)).append("\n");
                        break;
                    case "cut":
                        sb.append(new String(feedCmd, StandardCharsets.ISO_8859_1));
                        sb.append(new String(cutCmd, StandardCharsets.ISO_8859_1));
                        break;
                }
            }

            return sb.toString().getBytes(StandardCharsets.ISO_8859_1);
        }
    }

    /**
     * Cek apakah string hanya berisi karakter yang sama (untuk deteksi separator)
     */
    private boolean isAllSameChar(String s, char c) {
        if (s == null || s.isEmpty()) return false;
        for (int i = 0; i < s.length(); i++) {
            if (s.charAt(i) != c) return false;
        }
        return true;
    }

    /**
     * Buat string dengan karakter yang diulang sejumlah n kali
     */
    private String repeatChar(char c, int n) {
        StringBuilder sb = new StringBuilder(n);
        for (int i = 0; i < n; i++) {
            sb.append(c);
        }
        return sb.toString();
    }

    private void reportPrintSuccess(int printId) {
        reportStatus(printId, true, null);
    }

    private void reportPrintFailure(int printId, String error) {
        reportStatus(printId, false, error);
    }

    private void reportStatus(int printId, boolean success, String error) {
        if (socket != null && socket.connected()) {
            try {
                JSONObject data = new JSONObject();
                data.put("print_id", printId);
                if (!success) {
                    data.put("error", error);
                }
                socket.emit(success ? "print_complete" : "print_failed", data);
                return;
            } catch (Exception e) {
                Log.e(TAG, "Error reporting via WebSocket");
            }
        }

        if (serverUrl != null && authToken != null) {
            printExecutor.execute(() -> reportViaHttp(printId, success, error));
        }
    }

    private void reportViaHttp(int printId, boolean success, String error) {
        OkHttpClient client = new OkHttpClient.Builder()
                .connectTimeout(SOCKET_TIMEOUT, TimeUnit.MILLISECONDS)
                .build();

        try {
            String endpoint = success
                    ? serverUrl + "/api/pending-prints/" + printId + "/complete"
                    : serverUrl + "/api/pending-prints/" + printId + "/fail";

            Request.Builder requestBuilder = new Request.Builder()
                    .url(endpoint)
                    .header("Authorization", "Bearer " + authToken);

            if (success) {
                requestBuilder.post(RequestBody.create("", MediaType.parse("application/json")));
            } else {
                JSONObject json = new JSONObject();
                json.put("error_message", error);
                requestBuilder.post(RequestBody.create(
                        json.toString(), MediaType.parse("application/json")));
            }

            client.newCall(requestBuilder.build()).execute();
        } catch (Exception e) {
            Log.e(TAG, "HTTP report error: " + e.getMessage());
        }
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "Print Service",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Background print service");
            channel.setShowBadge(false);

            NotificationManager manager = getSystemService(NotificationManager.class);
            if (manager != null) {
                manager.createNotificationChannel(channel);
            }
        }
    }

    private Notification buildNotification(String status) {
        Intent intent = new Intent(this, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setContentTitle("🖨️ Dapoer Teras Obor - Print Service")
                .setContentText(status)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentIntent(pendingIntent)
                .setOngoing(true)
                .setSilent(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .build();
    }

    private void updateNotification(String status) {
        if (mainHandler != null) {
            mainHandler.post(() -> {
                NotificationManager manager = getSystemService(NotificationManager.class);
                if (manager != null && isServiceRunning.get()) {
                    manager.notify(NOTIFICATION_ID, buildNotification(status));
                }
            });
        }
    }

    private void closePrinterConnection() {
        synchronized (printerLock) {
            try {
                if (printerOutputStream != null) {
                    printerOutputStream.close();
                }
            } catch (IOException e) {
                Log.e(TAG, "Error closing stream");
            }

            try {
                if (bluetoothSocket != null) {
                    bluetoothSocket.close();
                }
            } catch (IOException e) {
                Log.e(TAG, "Error closing bluetooth");
            }

            try {
                if (lanSocket != null) {
                    lanSocket.close();
                }
            } catch (IOException e) {
                Log.e(TAG, "Error closing LAN socket");
            }

            printerOutputStream = null;
            bluetoothSocket = null;
            lanSocket = null;
            isPrinterConnected = false;
        }
    }

    private void scheduleReconnect() {
        if (isReconnecting.compareAndSet(false, true)) {
            // Reset flag pengecekan karena akan reconnect
            hasCheckedPendingAfterConnect = false;

            mainHandler.postDelayed(() -> {
                if (isServiceRunning.get() && !isPrinterConnected) {
                    Log.i(TAG, "Attempting printer reconnect...");
                    isReconnecting.set(false);
                    connectPrinter();
                }
            }, 5000);
        }
    }

    @Override
    public void onDestroy() {
        Log.i(TAG, "PrintService stopping...");
        isServiceRunning.set(false);

        if (periodicHandler != null && periodicCheckRunnable != null) {
            periodicHandler.removeCallbacks(periodicCheckRunnable);
            periodicHandler = null;
        }

        // Batalkan semua print jobs
        for (Future<?> future : printJobs.values()) {
            if (future != null && !future.isDone()) {
                future.cancel(true);
            }
        }
        printJobs.clear();

        // Kosongkan queue
        pendingPrintQueue.clear();
        retryJobs.clear();

        synchronized (socketLock) {
            if (socket != null) {
                socket.off();
                socket.disconnect();
                socket.close();
                socket = null;
            }
        }

        closePrinterConnection();

        shutdownExecutor(printExecutor, "Print");
        shutdownExecutor(networkExecutor, "Network");

        stopForeground(true);
        super.onDestroy();
    }

    private void shutdownExecutor(ExecutorService executor, String name) {
        if (executor != null && !executor.isShutdown()) {
            executor.shutdown();
            try {
                if (!executor.awaitTermination(SHUTDOWN_TIMEOUT, TimeUnit.MILLISECONDS)) {
                    executor.shutdownNow();
                }
            } catch (InterruptedException e) {
                executor.shutdownNow();
                Thread.currentThread().interrupt();
            }
        }
    }

    @Nullable
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}