package com.telegram.uploader.ui.screens

import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.telegram.uploader.data.db.AppDatabaseHelper
import com.telegram.uploader.network.TelegramApiClient
import com.telegram.uploader.ui.components.formatBytes
import com.telegram.uploader.ui.theme.*
import kotlinx.coroutines.launch
import java.io.File

@Composable
fun SettingsScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val dbHelper = remember { AppDatabaseHelper.getInstance(context) }
    val telegramClient = remember { TelegramApiClient.getInstance() }

    var botToken by remember { mutableStateOf(dbHelper.getSetting("bot_token", "")) }
    var isTestingConnection by remember { mutableStateOf(false) }
    var botStatusText by remember { mutableStateOf(dbHelper.getSetting("bot_status_info", "")) }
    var isConnected by remember { mutableStateOf(dbHelper.getSetting("bot_is_connected", "false") == "true") }
    var cacheBytes by remember { mutableStateOf(calculateDirSize(context.cacheDir) + (context.externalCacheDir?.let { calculateDirSize(it) } ?: 0L)) }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TelegramDarkBg)
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        // Section: Telegram Bot Auth
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = TelegramCardBg),
                shape = RoundedCornerShape(12.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, TelegramBorder)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text(
                        text = "Telegram Bot Authentication",
                        color = TextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = "Connect with your Bot Token from @BotFather to enable direct uploads.",
                        color = TextSecondary,
                        fontSize = 12.sp,
                        modifier = Modifier.padding(top = 2.dp, bottom = 12.dp)
                    )

                    OutlinedTextField(
                        value = botToken,
                        onValueChange = { botToken = it },
                        label = { Text("Bot Token (from @BotFather) *") },
                        placeholder = { Text("1234567890:ABCdefGhIJKlmNoPQRsTUVwxyZ") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = TelegramBlue,
                            unfocusedBorderColor = TelegramBorder,
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary
                        )
                    )

                    Spacer(modifier = Modifier.height(12.dp))

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        Button(
                            onClick = {
                                if (botToken.trim().isEmpty()) {
                                    Toast.makeText(context, "Please enter your bot token", Toast.LENGTH_SHORT).show()
                                    return@Button
                                }
                                scope.launch {
                                    isTestingConnection = true
                                    val res = telegramClient.getMe(botToken.trim())
                                    if (res.isSuccess) {
                                        val json = res.getOrThrow()
                                        val name = json.optString("first_name", "Bot")
                                        val username = json.optString("username", "")
                                        botStatusText = "Connected as: $name (@$username)"
                                        isConnected = true
                                        dbHelper.saveSetting("bot_token", botToken.trim())
                                        dbHelper.saveSetting("bot_status_info", botStatusText)
                                        dbHelper.saveSetting("bot_is_connected", "true")
                                        Toast.makeText(context, "Connected successfully!", Toast.LENGTH_SHORT).show()
                                    } else {
                                        val err = res.exceptionOrNull()?.message ?: "Connection failed"
                                        botStatusText = "Connection error: $err"
                                        isConnected = false
                                        Toast.makeText(context, err, Toast.LENGTH_LONG).show()
                                    }
                                    isTestingConnection = false
                                }
                            },
                            colors = ButtonDefaults.buttonColors(containerColor = TelegramBlue),
                            shape = RoundedCornerShape(8.dp),
                            enabled = !isTestingConnection,
                            modifier = Modifier.weight(1f)
                        ) {
                            Text(if (isTestingConnection) "Verifying..." else "Connect & Test Bot")
                        }

                        if (isConnected) {
                            OutlinedButton(
                                onClick = {
                                    botToken = ""
                                    botStatusText = ""
                                    isConnected = false
                                    dbHelper.saveSetting("bot_token", "")
                                    dbHelper.saveSetting("bot_status_info", "")
                                    dbHelper.saveSetting("bot_is_connected", "false")
                                },
                                shape = RoundedCornerShape(8.dp)
                            ) {
                                Text("Disconnect", color = AccentRed)
                            }
                        }
                    }

                    if (botStatusText.isNotEmpty()) {
                        Spacer(modifier = Modifier.height(10.dp))
                        Text(
                            text = if (isConnected) "✅ $botStatusText" else "❌ $botStatusText",
                            color = if (isConnected) AccentGreen else AccentRed,
                            fontSize = 12.sp,
                            fontWeight = FontWeight.Medium
                        )
                    }
                }
            }
        }

        // Section: Storage & Upload Cache
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = TelegramCardBg),
                shape = RoundedCornerShape(12.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, TelegramBorder)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text(
                        text = "💾 Storage & App Cache",
                        color = TextPrimary,
                        fontSize = 15.sp,
                        fontWeight = FontWeight.Bold
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    Text(
                        text = "Temporary Cache Size: ${formatBytes(cacheBytes)}",
                        color = TelegramLightBlue,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.SemiBold
                    )
                    Text(
                        text = "Clears temporary media picker and HTTP buffers without affecting your Telegram files, queued items, or phone gallery.",
                        color = TextMuted,
                        fontSize = 11.sp,
                        modifier = Modifier.padding(top = 4.dp, bottom = 12.dp)
                    )
                    Button(
                        onClick = {
                            val freed = clearDirectoryContents(context.cacheDir) +
                                (context.externalCacheDir?.let { clearDirectoryContents(it) } ?: 0L)
                            cacheBytes = calculateDirSize(context.cacheDir) +
                                (context.externalCacheDir?.let { calculateDirSize(it) } ?: 0L)
                            Toast.makeText(
                                context,
                                "Cleared ${formatBytes(freed)} of temporary app cache",
                                Toast.LENGTH_SHORT
                            ).show()
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = AccentRed.copy(alpha = 0.85f)),
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text("🧹 Clear Upload Cache (${formatBytes(cacheBytes)})", color = TextPrimary)
                    }
                }
            }
        }

        // Section: Engine Info
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = TelegramCardBg),
                shape = RoundedCornerShape(12.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, TelegramBorder)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text(
                        text = "About Native Android Engine",
                        color = TextPrimary,
                        fontSize = 15.sp,
                        fontWeight = FontWeight.Bold
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "• WorkManager Background Service: Uploads continue even when phone is locked or other apps are running.\n" +
                               "• Exact File Fidelity: Preserves lossless uncompressed byte-for-byte sizes.\n" +
                               "• Progressive Streaming: Sets supports_streaming=true for seamless chat playback.\n" +
                               "• Duplicate Safety: Automatically identifies and flags files already delivered.",
                        color = TextSecondary,
                        fontSize = 12.sp,
                        lineHeight = 18.sp
                    )
                }
            }
        }
    }
}

private fun calculateDirSize(dir: File?): Long {
    if (dir == null || !dir.exists()) return 0L
    return dir.walkTopDown().filter { it.isFile }.sumOf { it.length() }
}

private fun clearDirectoryContents(dir: File?): Long {
    if (dir == null || !dir.exists()) return 0L
    var freed = 0L
    dir.listFiles()?.forEach { child ->
        freed += calculateDirSize(child)
        child.deleteRecursively()
    }
    return freed
}
