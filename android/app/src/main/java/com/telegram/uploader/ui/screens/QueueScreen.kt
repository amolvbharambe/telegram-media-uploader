package com.telegram.uploader.ui.screens

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.work.*
import com.telegram.uploader.data.db.AppDatabaseHelper
import com.telegram.uploader.data.model.UploadTask
import com.telegram.uploader.ui.components.formatBytes
import com.telegram.uploader.ui.theme.*
import com.telegram.uploader.worker.UploadWorker
import kotlinx.coroutines.delay

@Composable
fun QueueScreen() {
    val context = LocalContext.current
    val dbHelper = remember { AppDatabaseHelper.getInstance(context) }
    var tasks by remember { mutableStateOf(listOf<UploadTask>()) }

    // Auto-refresh tasks every 1 second when active
    LaunchedEffect(Unit) {
        while (true) {
            tasks = dbHelper.getAllTasks()
            delay(1000)
        }
    }

    val completedCount = tasks.count { it.status == "completed" }
    val activeCount = tasks.count { it.status == "uploading" || it.status == "pending" || it.status == "retrying" }
    val totalUploadedBytes = tasks.filter { it.status == "completed" }.sumOf { it.fileSize }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TelegramDarkBg)
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp)
    ) {
        // Stats Overview Row
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(10.dp)
            ) {
                StatBox(
                    label = "Active",
                    value = activeCount.toString(),
                    sub = "In queue",
                    color = TelegramLightBlue,
                    modifier = Modifier.weight(1f)
                )
                StatBox(
                    label = "Delivered",
                    value = completedCount.toString(),
                    sub = "Delivered",
                    color = AccentGreen,
                    modifier = Modifier.weight(1f)
                )
                StatBox(
                    label = "Total Sent",
                    value = formatBytes(totalUploadedBytes),
                    sub = "Data uploaded",
                    color = TelegramBlue,
                    modifier = Modifier.weight(1.2f)
                )
            }
        }

        // Header with Refresh & Clear
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "Upload Queue (${tasks.size})",
                    color = TextPrimary,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold
                )
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    OutlinedButton(
                        onClick = { tasks = dbHelper.getAllTasks() },
                        shape = RoundedCornerShape(6.dp),
                        contentPadding = PaddingValues(horizontal = 8.dp, vertical = 2.dp)
                    ) {
                        Text("🔄 Refresh", fontSize = 11.sp, color = TelegramLightBlue)
                    }
                    if (completedCount > 0) {
                        Button(
                            onClick = {
                                dbHelper.clearCompletedTasks()
                                tasks = dbHelper.getAllTasks()
                            },
                            colors = ButtonDefaults.buttonColors(containerColor = TelegramSecondaryBg),
                            shape = RoundedCornerShape(6.dp),
                            contentPadding = PaddingValues(horizontal = 8.dp, vertical = 2.dp)
                        ) {
                            Text("Clear Finished", fontSize = 11.sp, color = TextSecondary)
                        }
                    }
                }
            }
        }

        if (tasks.isEmpty()) {
            item {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 40.dp),
                    contentAlignment = Alignment.Center
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("📬", fontSize = 36.sp)
                        Spacer(modifier = Modifier.height(8.dp))
                        Text("No uploads in queue", color = TextPrimary, fontSize = 15.sp, fontWeight = FontWeight.Bold)
                        Text("Selected files will appear here with live progress tracking.", color = TextMuted, fontSize = 12.sp)
                    }
                }
            }
        } else {
            items(tasks, key = { it.id }) { task ->
                TaskCard(
                    task = task,
                    onCancel = {
                        dbHelper.deleteTask(task.id)
                        tasks = dbHelper.getAllTasks()
                    },
                    onRetry = {
                        dbHelper.resetTaskForRetry(task.id)
                        val constraints = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
                        val req = OneTimeWorkRequestBuilder<UploadWorker>().setConstraints(constraints).build()
                        WorkManager.getInstance(context).enqueueUniqueWork(UploadWorker.WORK_NAME, ExistingWorkPolicy.REPLACE, req)
                        tasks = dbHelper.getAllTasks()
                    }
                )
            }
        }
    }
}

@Composable
private fun StatBox(
    label: String,
    value: String,
    sub: String,
    color: androidx.compose.ui.graphics.Color,
    modifier: Modifier = Modifier
) {
    Card(
        colors = CardDefaults.cardColors(containerColor = TelegramCardBg),
        shape = RoundedCornerShape(10.dp),
        border = androidx.compose.foundation.BorderStroke(1.dp, TelegramBorder),
        modifier = modifier
    ) {
        Column(modifier = Modifier.padding(10.dp)) {
            Text(text = label, color = TextMuted, fontSize = 10.sp, fontWeight = FontWeight.SemiBold)
            Text(text = value, color = color, fontSize = 16.sp, fontWeight = FontWeight.Bold)
            Text(text = sub, color = TextSecondary, fontSize = 9.sp)
        }
    }
}

@Composable
private fun TaskCard(
    task: UploadTask,
    onCancel: () -> Unit,
    onRetry: () -> Unit
) {
    val statusColor = when (task.status) {
        "completed" -> AccentGreen
        "uploading" -> TelegramBlue
        "retrying" -> AccentAmber
        "failed" -> AccentRed
        else -> TextSecondary
    }

    val icon = when (task.mediaType) {
        "video" -> "🎬"
        "photo" -> "🖼"
        "audio" -> "🎵"
        else -> "📄"
    }

    Card(
        colors = CardDefaults.cardColors(containerColor = TelegramCardBg),
        shape = RoundedCornerShape(10.dp),
        border = androidx.compose.foundation.BorderStroke(1.dp, TelegramBorder),
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(
                    modifier = Modifier.weight(1f),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Text(text = icon, fontSize = 18.sp)
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            text = task.fileName,
                            color = TextPrimary,
                            fontSize = 13.sp,
                            fontWeight = FontWeight.SemiBold,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis
                        )
                        Text(
                            text = "To: ${task.chatTitle.ifEmpty { task.targetChat }} • ${formatBytes(task.fileSize)}",
                            color = TextSecondary,
                            fontSize = 11.sp
                        )
                    }
                }

                // Status Badge
                Text(
                    text = task.status.uppercase(),
                    color = statusColor,
                    fontSize = 11.sp,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier
                        .clip(RoundedCornerShape(4.dp))
                        .background(statusColor.copy(alpha = 0.15f))
                        .padding(horizontal = 6.dp, vertical = 2.dp)
                )
            }

            Spacer(modifier = Modifier.height(10.dp))

            // Progress Bar
            LinearProgressIndicator(
                progress = { task.progressPercent / 100f },
                modifier = Modifier
                    .fillMaxWidth()
                    .height(6.dp)
                    .clip(RoundedCornerShape(3.dp)),
                color = if (task.status == "completed") AccentGreen else TelegramBlue,
                trackColor = TelegramSecondaryBg,
            )

            Spacer(modifier = Modifier.height(6.dp))

            // Progress Metrics & Actions
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "${formatBytes(task.bytesUploaded)} of ${formatBytes(task.fileSize)} (${task.progressPercent}%)",
                    color = TextSecondary,
                    fontSize = 11.sp
                )

                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    if (task.status == "failed" || task.status == "retrying") {
                        TextButton(
                            onClick = onRetry,
                            contentPadding = PaddingValues(horizontal = 6.dp, vertical = 0.dp),
                            modifier = Modifier.height(26.dp)
                        ) {
                            Text("Retry", fontSize = 11.sp, color = TelegramLightBlue)
                        }
                    }

                    if (task.status != "completed" && task.status != "cancelled") {
                        TextButton(
                            onClick = onCancel,
                            contentPadding = PaddingValues(horizontal = 6.dp, vertical = 0.dp),
                            modifier = Modifier.height(26.dp)
                        ) {
                            Text("Cancel", fontSize = 11.sp, color = AccentRed)
                        }
                    } else if (task.status == "completed") {
                        Text(
                            text = "Delivered ✓",
                            color = AccentGreen,
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Medium
                        )
                    }
                }
            }

            // Error notice if retrying or failed
            if (!task.errorMessage.isNullOrEmpty() && task.status != "completed") {
                Spacer(modifier = Modifier.height(6.dp))
                Text(
                    text = "⚡ ${task.errorMessage} (Attempt ${task.retryCount}/${task.maxRetries})",
                    color = AccentAmber,
                    fontSize = 10.sp
                )
            }
        }
    }
}
