package com.dapoerterasobor.printservice;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.util.Log;

import androidx.core.app.NotificationCompat;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

public class MyFirebaseMessagingService extends FirebaseMessagingService {
    private static final String TAG = "FCMService";
    private static final String CHANNEL_ID = "kasir_orders";
    private static final String CHANNEL_NAME = "Pesanan";
    private static final String DEFAULT_TARGET_URL = "/orders";
    private static final java.util.concurrent.atomic.AtomicInteger notificationIdCounter =
            new java.util.concurrent.atomic.AtomicInteger(0);

    @Override
    public void onNewToken(String token) {
        super.onNewToken(token);
        Log.d(TAG, "New FCM token: " + token);
        SharedPreferences prefs = getSharedPreferences("PrintServicePrefs", MODE_PRIVATE);
        prefs.edit().putString("fcm_token", token).apply();
    }

    @Override
    public void onMessageReceived(RemoteMessage remoteMessage) {
        super.onMessageReceived(remoteMessage);
        Log.d(TAG, "Message received from: " + remoteMessage.getFrom());

        String title = "Pesanan Baru";
        String body = "";
        String targetUrl = DEFAULT_TARGET_URL;

        if (remoteMessage.getNotification() != null) {
            title = remoteMessage.getNotification().getTitle() != null
                    ? remoteMessage.getNotification().getTitle() : title;
            body = remoteMessage.getNotification().getBody() != null
                    ? remoteMessage.getNotification().getBody() : body;
        }

        if (remoteMessage.getData().size() > 0) {
            if (remoteMessage.getData().containsKey("title")) {
                title = remoteMessage.getData().get("title");
            }
            if (remoteMessage.getData().containsKey("body")) {
                body = remoteMessage.getData().get("body");
            }
            if (remoteMessage.getData().containsKey("url")) {
                targetUrl = remoteMessage.getData().get("url");
            }
        }

        showNotification(title, body, targetUrl);
    }

    private void showNotification(String title, String body, String targetUrl) {
        NotificationManager notificationManager =
                (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, CHANNEL_NAME, NotificationManager.IMPORTANCE_HIGH);
            channel.setDescription("Notifikasi pesanan masuk");
            channel.enableVibration(true);
            channel.setVibrationPattern(new long[]{0, 500, 200, 500});
            notificationManager.createNotificationChannel(channel);
        }

        Intent intent = new Intent(this, MainActivity.class);
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        intent.putExtra("target_url", targetUrl);

        PendingIntent pendingIntent = PendingIntent.getActivity(this, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Uri defaultSoundUri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION);

        NotificationCompat.Builder notificationBuilder =
                new NotificationCompat.Builder(this, CHANNEL_ID)
                        .setSmallIcon(android.R.drawable.ic_dialog_info)
                        .setContentTitle(title)
                        .setContentText(body)
                        .setAutoCancel(true)
                        .setSound(defaultSoundUri)
                        .setVibrate(new long[]{0, 500, 200, 500})
                        .setPriority(NotificationCompat.PRIORITY_HIGH)
                        .setContentIntent(pendingIntent)
                        .setStyle(new NotificationCompat.BigTextStyle().bigText(body));

        int notificationId = notificationIdCounter.incrementAndGet();
        notificationManager.notify(notificationId, notificationBuilder.build());
    }
}
