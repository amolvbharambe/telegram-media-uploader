package com.telegram.uploader.ui.screens

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.work.*
import com.telegram.uploader.data.db.AppDatabaseHelper
import com.telegram.uploader.data.model.TelegramDialog
import com.telegram.uploader.data.model.UploadTask
import com.telegram.uploader.network.TelegramApiClient
import com.telegram.uploader.ui.components.SelectedMedia
import com.telegram.uploader.ui.components.SelectedMediaRow
import com.telegram.uploader.ui.components.formatBytes
import com.telegram.uploader.ui.theme.*
import com.telegram.uploader.worker.UploadWorker
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun UploadScreen(
    initialUris: List<Uri> = emptyList(),
    onNavigateToQueue: () -> Unit
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val dbHelper = remember { AppDatabaseHelper.getInstance(context) }
    val telegramClient = remember { TelegramApiClient.getInstance() }

    var selectedList by remember { mutableStateOf(listOf<SelectedMedia>()) }
    var targetChat by remember { mutableStateOf(dbHelper.getSetting("default_target_chat", "")) }
    var chatTitle by remember { mutableStateOf(dbHelper.getSetting("default_chat_title", "")) }
    var caption by remember { mutableStateOf("") }
    var mediaType by remember { mutableStateOf("auto") } // "auto", "video", "audio", "photo", "document"
    var forceDocument by remember { mutableStateOf(false) }
    var autoSkipDuplicates by remember { mutableStateOf(true) }

    var dialogsList by remember { mutableStateOf(dbHelper.getKnownDialogs()) }
    var dialogsExpanded by remember { mutableStateOf(false) }
    var isFetchingDialogs by remember { mutableStateOf(false) }
    var isCheckingDuplicates by remember { mutableStateOf(false) }
    var showDuplicateDialog by remember { mutableStateOf(false) }

    suspend fun refreshDialogs() {
        val botToken = dbHelper.getSetting("bot_token", "")
        if (botToken.isNotEmpty()) {
            isFetchingDialogs = true
            val cached = dbHelper.getKnownDialogs()
            if (cached.isNotEmpty()) dialogsList = cached
            val recentChats = dbHelper.getRecentTargetChats()
            val res = telegramClient.getRecentDialogs(botToken, recentChats)
            if (res.isSuccess) {
                val live = res.getOrDefault(emptyList())
                if (live.isNotEmpty()) {
                    dbHelper.saveKnownDialogs(live)
                    dialogsList = dbHelper.getKnownDialogs()
                }
                if (targetChat.isEmpty() && dialogsList.isNotEmpty()) {
                    targetChat = dialogsList.first().id
                    chatTitle = dialogsList.first().title
                }
            }
            isFetchingDialogs = false
        }
    }

    // Media Pickers
    val photoVideoPicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.PickMultipleVisualMedia()
    ) { uris ->
        if (uris.isNotEmpty()) {
            val extracted = uris.map { uri -> extractMediaInfo(context, uri) }
            selectedList = (selectedList + extracted).distinctBy { it.name + it.size }
            checkDuplicates(context, dbHelper, targetChat, selectedList) { updated ->
                selectedList = updated
            }
        }
    }

    val documentPicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.OpenMultipleDocuments()
    ) { uris ->
        if (uris.isNotEmpty()) {
            val extracted = uris.map { uri -> extractMediaInfo(context, uri) }
            selectedList = (selectedList + extracted).distinctBy { it.name + it.size }
            checkDuplicates(context, dbHelper, targetChat, selectedList) { updated ->
                selectedList = updated
            }
        }
    }

    // Handle incoming share sheet URIs
    LaunchedEffect(initialUris) {
        if (initialUris.isNotEmpty()) {
            val extracted = initialUris.map { uri -> extractMediaInfo(context, uri) }
            selectedList = (selectedList + extracted).distinctBy { it.name + it.size }
            checkDuplicates(context, dbHelper, targetChat, selectedList) { updated ->
                selectedList = updated
            }
        }
    }

    // Load recent & persistent Telegram dialogs on start
    LaunchedEffect(Unit) {
        refreshDialogs()
    }

    // Duplicate check on target chat change
    LaunchedEffect(targetChat) {
        if (targetChat.isNotEmpty() && selectedList.isNotEmpty()) {
            checkDuplicates(context, dbHelper, targetChat, selectedList) { updated ->
                selectedList = updated
            }
        }
    }

    val duplicateCount = selectedList.count { it.isDuplicate }
    val totalBytes = selectedList.sumOf { it.size }

    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .background(TelegramDarkBg)
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        // Section 1: Media Selection Card
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = TelegramCardBg),
                shape = RoundedCornerShape(12.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, TelegramBorder)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text(
                        text = "Select Media to Upload",
                        color = TextPrimary,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.Bold
                    )
                    Text(
                        text = "Videos, Photos, Audio or Documents (lossless & streamable)",
                        color = TextSecondary,
                        fontSize = 12.sp,
                        modifier = Modifier.padding(top = 2.dp, bottom = 12.dp)
                    )

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        Button(
                            onClick = {
                                photoVideoPicker.launch(
                                    PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageAndVideo)
                                )
                            },
                            colors = ButtonDefaults.buttonColors(containerColor = TelegramBlue),
                            shape = RoundedCornerShape(8.dp),
                            modifier = Modifier.weight(1f)
                        ) {
                            Text("🖼 Pick Photos/Videos", fontSize = 12.sp)
                        }

                        OutlinedButton(
                            onClick = {
                                documentPicker.launch(arrayOf("*/*"))
                            },
                            shape = RoundedCornerShape(8.dp),
                            modifier = Modifier.weight(1f)
                        ) {
                            Text("📄 Browse Files", fontSize = 12.sp, color = TelegramLightBlue)
                        }
                    }

                    // Duplicate warning banner
                    if (duplicateCount > 0) {
                        Spacer(modifier = Modifier.height(10.dp))
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(8.dp))
                                .background(AccentAmber.copy(alpha = 0.15f))
                                .border(1.dp, AccentAmber.copy(alpha = 0.4f), RoundedCornerShape(8.dp))
                                .padding(horizontal = 10.dp, vertical = 6.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text(
                                text = "⚠️ $duplicateCount file${if (duplicateCount > 1) "s" else ""} already in group",
                                color = AccentAmber,
                                fontSize = 12.sp,
                                fontWeight = FontWeight.SemiBold
                            )
                            Button(
                                onClick = {
                                    selectedList = selectedList.filter { !it.isDuplicate }
                                },
                                colors = ButtonDefaults.buttonColors(containerColor = AccentAmber),
                                contentPadding = PaddingValues(horizontal = 8.dp, vertical = 2.dp),
                                modifier = Modifier.height(28.dp),
                                shape = RoundedCornerShape(4.dp)
                            ) {
                                Text("⚡ Skip Duplicates", fontSize = 11.sp, color = TelegramDarkBg)
                            }
                        }
                    }

                    // Selected files summary
                    if (selectedList.isNotEmpty()) {
                        Spacer(modifier = Modifier.height(12.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically
                        ) {
                            Text(
                                text = "${selectedList.size} file${if (selectedList.size > 1) "s" else ""} queued (${formatBytes(totalBytes)})",
                                color = TelegramLightBlue,
                                fontSize = 13.sp,
                                fontWeight = FontWeight.Bold
                            )
                            TextButton(onClick = { selectedList = emptyList() }) {
                                Text("Clear All", color = AccentRed, fontSize = 12.sp)
                            }
                        }
                    }
                }
            }
        }

        // List of selected files
        if (selectedList.isNotEmpty()) {
            itemsIndexed(selectedList) { index, item ->
                SelectedMediaRow(
                    item = item,
                    onRemove = {
                        selectedList = selectedList.toMutableList().also { it.removeAt(index) }
                    }
                )
            }
        }

        // Section 2: Destination & Options Card
        item {
            Card(
                colors = CardDefaults.cardColors(containerColor = TelegramCardBg),
                shape = RoundedCornerShape(12.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, TelegramBorder)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Text(
                            text = "Destination Chat / Group *",
                            color = TextPrimary,
                            fontSize = 14.sp,
                            fontWeight = FontWeight.Bold
                        )
                        TextButton(
                            onClick = { showDuplicateDialog = true },
                            contentPadding = PaddingValues(horizontal = 6.dp, vertical = 0.dp)
                        ) {
                            Text("🔍 Check Duplicates", fontSize = 11.sp, color = TelegramLightBlue)
                        }
                    }

                    // Known Telegram dialogs dropdown + refresh button
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(top = 4.dp, bottom = 8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        ExposedDropdownMenuBox(
                            expanded = dialogsExpanded,
                            onExpandedChange = { dialogsExpanded = !dialogsExpanded },
                            modifier = Modifier.weight(1f)
                        ) {
                            OutlinedTextField(
                                value = dialogsList.find { it.id == targetChat }?.let { "👥 ${it.title}" }
                                    ?: if (dialogsList.isEmpty()) "No saved groups yet" else "-- Select Telegram Group --",
                                onValueChange = {},
                                readOnly = true,
                                trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = dialogsExpanded) },
                                modifier = Modifier
                                    .menuAnchor(MenuAnchorType.PrimaryNotEditable)
                                    .fillMaxWidth(),
                                singleLine = true,
                                colors = OutlinedTextFieldDefaults.colors(
                                    focusedBorderColor = TelegramBlue,
                                    unfocusedBorderColor = TelegramBorder,
                                    focusedTextColor = TextPrimary,
                                    unfocusedTextColor = TextPrimary
                                )
                            )
                            ExposedDropdownMenu(
                                expanded = dialogsExpanded,
                                onDismissRequest = { dialogsExpanded = false },
                                modifier = Modifier.background(TelegramCardBg)
                            ) {
                                dialogsList.forEach { dialog ->
                                    DropdownMenuItem(
                                        text = {
                                            Text(
                                                text = "${if (dialog.type == "channel") "📢" else "👥"} ${dialog.title}",
                                                color = TextPrimary,
                                                fontSize = 13.sp
                                            )
                                        },
                                        onClick = {
                                            targetChat = dialog.id
                                            chatTitle = dialog.title
                                            dialogsExpanded = false
                                        }
                                    )
                                }
                            }
                        }

                        OutlinedButton(
                            onClick = {
                                scope.launch { refreshDialogs() }
                            },
                            enabled = !isFetchingDialogs,
                            shape = RoundedCornerShape(8.dp),
                            contentPadding = PaddingValues(horizontal = 10.dp, vertical = 12.dp)
                        ) {
                            Text(if (isFetchingDialogs) "..." else "🔄", fontSize = 14.sp)
                        }
                    }

                    // Chat selection or manual entry
                    OutlinedTextField(
                        value = targetChat,
                        onValueChange = { newVal ->
                            targetChat = newVal
                            chatTitle = dialogsList.find { it.id == newVal.trim() }?.title ?: ""
                        },
                        label = { Text("Target Chat ID or @channel") },
                        placeholder = { Text("e.g. -100123456789 or @mychannel") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true,
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = TelegramBlue,
                            unfocusedBorderColor = TelegramBorder,
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary
                        )
                    )

                    Spacer(modifier = Modifier.height(10.dp))

                    // Caption
                    OutlinedTextField(
                        value = caption,
                        onValueChange = { caption = it },
                        label = { Text("Caption (Optional)") },
                        modifier = Modifier.fillMaxWidth(),
                        maxLines = 3,
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = TelegramBlue,
                            unfocusedBorderColor = TelegramBorder,
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary
                        )
                    )

                    Spacer(modifier = Modifier.height(12.dp))

                    // Auto-skip duplicates checkbox
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier
                            .fillMaxWidth()
                            .clickable { autoSkipDuplicates = !autoSkipDuplicates }
                    ) {
                        Checkbox(
                            checked = autoSkipDuplicates,
                            onCheckedChange = { autoSkipDuplicates = it },
                            colors = CheckboxDefaults.colors(checkedColor = TelegramBlue)
                        )
                        Column(modifier = Modifier.padding(start = 6.dp)) {
                            Text(
                                text = "🛡️ Auto-skip duplicate files",
                                color = TextPrimary,
                                fontSize = 13.sp,
                                fontWeight = FontWeight.Medium
                            )
                            Text(
                                text = "Skips files already delivered to this destination",
                                color = TextMuted,
                                fontSize = 11.sp
                            )
                        }
                    }

                    // Streaming indicator
                    Spacer(modifier = Modifier.height(8.dp))
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(6.dp))
                            .background(AccentGreen.copy(alpha = 0.12f))
                            .padding(8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(6.dp)
                    ) {
                        Text("▶️", fontSize = 14.sp)
                        Text(
                            text = "Instant Streaming Enabled: Videos stream inline in Telegram.",
                            color = AccentGreen,
                            fontSize = 11.sp
                        )
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Queue Upload Button
                    Button(
                        onClick = {
                            if (targetChat.trim().isEmpty()) {
                                Toast.makeText(context, "Please enter a destination chat ID", Toast.LENGTH_SHORT).show()
                                return@Button
                            }
                            if (selectedList.isEmpty()) {
                                Toast.makeText(context, "Please select at least one media file", Toast.LENGTH_SHORT).show()
                                return@Button
                            }

                            val botToken = dbHelper.getSetting("bot_token", "")
                            if (botToken.isEmpty()) {
                                Toast.makeText(context, "Please configure your Bot Token in Settings first", Toast.LENGTH_LONG).show()
                                return@Button
                            }

                            // Save default chat and remember chat in persistent cache
                            val cleanChat = targetChat.trim()
                            dbHelper.saveSetting("default_target_chat", cleanChat)
                            dbHelper.saveSetting("default_chat_title", chatTitle)
                            if (dialogsList.none { it.id == cleanChat }) {
                                dbHelper.saveKnownDialogs(
                                    listOf(
                                        TelegramDialog(
                                            id = cleanChat,
                                            title = chatTitle.ifBlank { cleanChat },
                                            type = "group"
                                        )
                                    )
                                )
                            }

                            var queuedCount = 0
                            var skippedCount = 0

                            selectedList.forEach { media ->
                                if (autoSkipDuplicates && media.isDuplicate) {
                                    skippedCount++
                                } else {
                                    val task = UploadTask(
                                        uriString = media.uriString,
                                        fileName = media.name,
                                        fileSize = media.size,
                                        mimeType = media.mimeType,
                                        mediaType = media.mediaType,
                                        forceDocument = forceDocument,
                                        targetChat = cleanChat,
                                        chatTitle = chatTitle,
                                        caption = caption.trim()
                                    )
                                    dbHelper.insertTask(task)
                                    queuedCount++
                                }
                            }

                            // Enqueue WorkManager job with Network constraint without cancelling running batches
                            val constraints = Constraints.Builder()
                                .setRequiredNetworkType(NetworkType.CONNECTED)
                                .build()

                            val uploadWorkRequest = OneTimeWorkRequestBuilder<UploadWorker>()
                                .setConstraints(constraints)
                                .build()

                            WorkManager.getInstance(context).enqueueUniqueWork(
                                UploadWorker.WORK_NAME,
                                ExistingWorkPolicy.APPEND,
                                uploadWorkRequest
                            )

                            val toastMsg = if (skippedCount > 0) {
                                "Queued $queuedCount file(s) ($skippedCount duplicate(s) skipped)"
                            } else {
                                "Queued $queuedCount file(s) for background upload!"
                            }
                            Toast.makeText(context, toastMsg, Toast.LENGTH_SHORT).show()

                            selectedList = emptyList()
                            onNavigateToQueue()
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = TelegramBlue),
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(48.dp)
                    ) {
                        Text(
                            text = "🚀 Start / Queue Upload",
                            fontSize = 15.sp,
                            fontWeight = FontWeight.Bold
                        )
                    }
                }
            }
        }
    }

    // Duplicate Scanner Dialog
    if (showDuplicateDialog) {
        val completedInGroup = remember { dbHelper.getCompletedTasksByChat(targetChat) }
        AlertDialog(
            onDismissRequest = { showDuplicateDialog = false },
            title = {
                Text("🔍 Group Media Scanner", fontWeight = FontWeight.Bold, color = TextPrimary)
            },
            text = {
                Column {
                    Text(
                        text = "Destination: $targetChat",
                        fontSize = 12.sp,
                        color = TextSecondary
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "Total Delivered to this Group: ${completedInGroup.size} files",
                        fontSize = 13.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = TelegramLightBlue
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = "All selected media is verified against this local index to prevent duplicate uploads.",
                        fontSize = 12.sp,
                        color = TextMuted
                    )
                }
            },
            confirmButton = {
                TextButton(onClick = { showDuplicateDialog = false }) {
                    Text("Close", color = TelegramBlue)
                }
            },
            containerColor = TelegramCardBg
        )
    }
}

private fun extractMediaInfo(context: Context, uri: Uri): SelectedMedia {
    var name = "file_${System.currentTimeMillis()}"
    var size: Long = 0
    val mimeType = context.contentResolver.getType(uri) ?: "application/octet-stream"

    context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
        val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
        val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
        if (cursor.moveToFirst()) {
            if (nameIndex != -1) name = cursor.getString(nameIndex) ?: name
            if (sizeIndex != -1) size = cursor.getLong(sizeIndex)
        }
    }

    val mediaType = when {
        mimeType.startsWith("video/") || name.endsWith(".mp4", true) || name.endsWith(".mkv", true) || name.endsWith(".mov", true) -> "video"
        mimeType.startsWith("image/") || name.endsWith(".jpg", true) || name.endsWith(".png", true) || name.endsWith(".jpeg", true) -> "photo"
        mimeType.startsWith("audio/") || name.endsWith(".mp3", true) || name.endsWith(".m4a", true) -> "audio"
        else -> "document"
    }

    return SelectedMedia(
        uriString = uri.toString(),
        name = name,
        size = size,
        mimeType = mimeType,
        mediaType = mediaType
    )
}

private fun checkDuplicates(
    context: Context,
    db: AppDatabaseHelper,
    targetChat: String,
    items: List<SelectedMedia>,
    onResult: (List<SelectedMedia>) -> Unit
) {
    if (targetChat.isEmpty()) {
        onResult(items.map { it.copy(isDuplicate = false, duplicateReason = "") })
        return
    }

    val existing = db.getCompletedTasksByChat(targetChat)
    val existingSet = existing.map { (it.fileName.trim().lowercase() to it.fileSize) }.toSet()

    val updated = items.map { item ->
        val key = item.name.trim().lowercase() to item.size
        val isDup = key in existingSet
        val reason = if (isDup) "Already delivered to this group" else ""
        item.copy(isDuplicate = isDup, duplicateReason = reason)
    }
    onResult(updated)
}
