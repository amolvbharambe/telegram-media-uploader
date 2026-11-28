package com.telegram.uploader.network

import android.content.Context
import android.net.Uri
import com.telegram.uploader.data.model.TelegramDialog
import com.telegram.uploader.data.model.UploadTask
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

class TelegramApiClient private constructor() {

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)
        .writeTimeout(90, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    companion object {
        private const val BASE_URL = "https://api.telegram.org/bot"

        @Volatile
        private var instance: TelegramApiClient? = null

        fun getInstance(): TelegramApiClient {
            return instance ?: synchronized(this) {
                instance ?: TelegramApiClient().also { instance = it }
            }
        }
    }

    suspend fun getMe(botToken: String): Result<JSONObject> = withContext(Dispatchers.IO) {
        try {
            val url = "$BASE_URL$botToken/getMe"
            val request = Request.Builder().url(url).get().build()
            val response = httpClient.newCall(request).execute()
            val body = response.body?.string() ?: ""

            if (!response.isSuccessful) {
                return@withContext Result.failure(IOException("Telegram HTTP ${response.code}: $body"))
            }

            val json = JSONObject(body)
            if (json.optBoolean("ok")) {
                Result.success(json.getJSONObject("result"))
            } else {
                Result.failure(IOException(json.optString("description", "Unknown error")))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun getRecentDialogs(
        botToken: String,
        fallbackChatIds: List<String> = emptyList()
    ): Result<List<TelegramDialog>> = withContext(Dispatchers.IO) {
        try {
            val dialogsMap = linkedMapOf<String, TelegramDialog>()

            try {
                val url = "$BASE_URL$botToken/getUpdates?limit=100"
                val request = Request.Builder().url(url).get().build()
                val response = httpClient.newCall(request).execute()
                val body = response.body?.string() ?: ""
                if (response.isSuccessful) {
                    val json = JSONObject(body)
                    if (json.optBoolean("ok")) {
                        val updates = json.getJSONArray("result")
                        for (i in 0 until updates.length()) {
                            val update = updates.getJSONObject(i)
                            val chatObj = update.optJSONObject("message")?.optJSONObject("chat")
                                ?: update.optJSONObject("channel_post")?.optJSONObject("chat")
                                ?: update.optJSONObject("my_chat_member")?.optJSONObject("chat")

                            if (chatObj != null) {
                                val chatId = chatObj.optLong("id").toString()
                                val title = chatObj.optString("title").ifEmpty {
                                    val first = chatObj.optString("first_name", "")
                                    val last = chatObj.optString("last_name", "")
                                    "$first $last".trim().ifEmpty { chatId }
                                }
                                val type = chatObj.optString("type", "group")
                                dialogsMap[chatId] = TelegramDialog(id = chatId, title = title, type = type)
                            }
                        }
                    }
                }
            } catch (_: Exception) {
                // Continue to cached getChat lookups even if getUpdates fails or is rate-limited
            }

            // Refresh all previously known/cached target chats via getChat even if getUpdates expired
            for (cid in fallbackChatIds) {
                if (cid.isNotBlank() && !dialogsMap.containsKey(cid)) {
                    try {
                        val cUrl = "$BASE_URL$botToken/getChat?chat_id=$cid"
                        val cReq = Request.Builder().url(cUrl).get().build()
                        val cResp = httpClient.newCall(cReq).execute()
                        val cBody = cResp.body?.string() ?: ""
                        if (cResp.isSuccessful) {
                            val cJson = JSONObject(cBody)
                            if (cJson.optBoolean("ok")) {
                                val cObj = cJson.getJSONObject("result")
                                val title = cObj.optString("title").ifEmpty { cid }
                                val type = cObj.optString("type", "group")
                                dialogsMap[cid] = TelegramDialog(id = cid, title = title, type = type)
                            }
                        }
                    } catch (_: Exception) {
                        // Ignore individual lookup errors offline
                    }
                }
            }

            Result.success(dialogsMap.values.toList())
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    suspend fun uploadMedia(
        context: Context,
        botToken: String,
        task: UploadTask,
        onProgress: (bytesUploaded: Long, totalBytes: Long) -> Unit
    ): Result<Long> = withContext(Dispatchers.IO) {
        try {
            val uri = Uri.parse(task.uriString)
            val isDoc = task.forceDocument || task.mediaType == "document"
            val endpoint = when {
                isDoc -> "sendDocument"
                task.mediaType == "video" -> "sendVideo"
                task.mediaType == "audio" -> "sendAudio"
                task.mediaType == "photo" -> "sendPhoto"
                else -> "sendDocument"
            }

            val fileParamName = when (endpoint) {
                "sendVideo" -> "video"
                "sendPhoto" -> "photo"
                "sendAudio" -> "audio"
                else -> "document"
            }

            val progressBody = ProgressRequestBody(
                context = context,
                uri = uri,
                mimeType = task.mimeType,
                totalBytes = task.fileSize,
                onProgress = onProgress
            )

            val multipartBuilder = MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("chat_id", task.targetChat)

            if (task.caption.isNotEmpty()) {
                multipartBuilder.addFormDataPart("caption", task.caption)
            }

            // STREAMING ATTRIBUTE: Enable Telegram inline progressive player streaming for video
            if (endpoint == "sendVideo" && !task.forceDocument) {
                multipartBuilder.addFormDataPart("supports_streaming", "true")
            }

            multipartBuilder.addFormDataPart(fileParamName, task.fileName, progressBody)

            val request = Request.Builder()
                .url("$BASE_URL$botToken/$endpoint")
                .post(multipartBuilder.build())
                .build()

            val response = httpClient.newCall(request).execute()
            val responseBody = response.body?.string() ?: ""

            if (!response.isSuccessful) {
                val errorMsg = try {
                    JSONObject(responseBody).optString("description", "HTTP ${response.code}")
                } catch (_: Exception) {
                    "HTTP ${response.code}: $responseBody"
                }
                return@withContext Result.failure(IOException(errorMsg))
            }

            val json = JSONObject(responseBody)
            if (json.optBoolean("ok")) {
                val resultObj = json.getJSONObject("result")
                val messageId = resultObj.optLong("message_id", 0L)
                Result.success(messageId)
            } else {
                val desc = json.optString("description", "Upload failed")
                Result.failure(IOException(desc))
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }
}
