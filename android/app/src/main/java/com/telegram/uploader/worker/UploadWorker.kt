package com.telegram.uploader.worker

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.telegram.uploader.MainActivity
import com.telegram.uploader.data.db.AppDatabaseHelper
import com.telegram.uploader.network.TelegramApiClient

class UploadWorker(
    private val appContext: Context,
    workerParams: WorkerParameters
) : CoroutineWorker(appContext, workerParams) {

    private val dbHelper = AppDatabaseHelper.getInstance(appContext)
    private val telegramClient = TelegramApiClient.getInstance()
    private val notificationManager =
        appContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    companion object {
        const val CHANNEL_ID = "telegram_upload_channel"
        const val NOTIFICATION_ID = 1001
        const val WORK_NAME = "TelegramUploadWork"
    }

    override suspend fun doWork(): Result {
        createNotificationChannel()

        val botToken = dbHelper.getSetting("bot_token", "")
        if (botToken.isEmpty()) {
            return Result.failure(workDataOf("error" to "Bot token not configured"))
        }

        // Show initial foreground notification
        setForeground(createForegroundInfo("Preparing uploads...", 0, 0, 0))

        // Recover any tasks stuck in 'uploading' if an earlier upload stopped unexpectedly
        dbHelper.recoverInterruptedTasks()

        var hasFailures = false

        while (true) {
            val task = dbHelper.getNextPendingTask() ?: break
            var lastNotifyTime = 0L

            try {
                dbHelper.updateTaskStatus(task.id, "uploading")

                val result = telegramClient.uploadMedia(
                    context = appContext,
                    botToken = botToken,
                    task = task,
                    onProgress = { uploaded, total ->
                        val now = System.currentTimeMillis()
                        if (now - lastNotifyTime >= 300 || uploaded >= total) {
                            lastNotifyTime = now
                            if (dbHelper.getTask(task.id) == null) return@uploadMedia
                            val percent = if (total > 0) ((uploaded.toDouble() / total) * 100).toInt() else 0
                            dbHelper.updateTaskProgress(task.id, uploaded)

                            // Update foreground notification with live progress
                            try {
                                notificationManager.notify(
                                    NOTIFICATION_ID,
                                    buildNotification(
                                        title = "Uploading ${task.fileName}",
                                        text = "${formatBytes(uploaded)} / ${formatBytes(total)} ($percent%)",
                                        percent = percent,
                                        ongoing = true
                                    )
                                )
                            } catch (_: Exception) {}
                        }
                    }
                )

                // If user removed the task mid-flight, skip updating its state
                if (dbHelper.getTask(task.id) == null) continue

                if (result.isSuccess) {
                    val messageId = result.getOrNull()
                    dbHelper.updateTaskStatus(task.id, "completed", messageId = messageId)
                } else {
                    val error = result.exceptionOrNull()?.message ?: "Unknown upload failure"
                    val canRetry = dbHelper.incrementRetryCount(task.id, error)
                    if (canRetry) {
                        hasFailures = true
                    }
                }
            } catch (e: Exception) {
                if (dbHelper.getTask(task.id) == null) continue
                val canRetry = dbHelper.incrementRetryCount(task.id, e.message ?: "Exception occurred")
                if (canRetry) {
                    hasFailures = true
                }
            }
        }

        // Final notification update
        val activeCount = dbHelper.getActiveAndPendingTasks().size
        if (activeCount == 0) {
            notificationManager.notify(
                NOTIFICATION_ID,
                buildNotification(
                    title = "Uploads Completed",
                    text = "All media successfully delivered to Telegram!",
                    percent = 100,
                    ongoing = false
                )
            )
        }

        return if (hasFailures) Result.retry() else Result.success()
    }

    private fun createForegroundInfo(
        title: String,
        uploaded: Long,
        total: Long,
        percent: Int
    ): ForegroundInfo {
        val notification = buildNotification(
            title = title,
            text = if (total > 0) "${formatBytes(uploaded)} of ${formatBytes(total)}" else "Starting...",
            percent = percent,
            ongoing = true
        )

        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            ForegroundInfo(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            )
        } else {
            ForegroundInfo(NOTIFICATION_ID, notification)
        }
    }

    private fun buildNotification(
        title: String,
        text: String,
        percent: Int,
        ongoing: Boolean
    ) = NotificationCompat.Builder(appContext, CHANNEL_ID)
        .setContentTitle(title)
        .setContentText(text)
        .setSmallIcon(android.R.drawable.stat_sys_upload)
        .setOngoing(ongoing)
        .setOnlyAlertOnce(true)
        .setProgress(100, percent, percent == 0)
        .setContentIntent(
            PendingIntent.getActivity(
                appContext,
                0,
                Intent(appContext, MainActivity::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
        )
        .build()

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Telegram Upload Service",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Shows progress of background media uploads to Telegram"
            }
            notificationManager.createNotificationChannel(channel)
        }
    }

    private fun formatBytes(bytes: Long): String {
        if (bytes <= 0) return "0 B"
        val units = arrayOf("B", "KB", "MB", "GB", "TB")
        val digitGroups = (Math.log10(bytes.toDouble()) / Math.log10(1024.0)).toInt().coerceIn(0, units.size - 1)
        val value = bytes / Math.pow(1024.0, digitGroups.toDouble())
        return "%.1f %s".format(value, units[digitGroups])
    }
}
