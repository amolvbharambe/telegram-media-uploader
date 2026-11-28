package com.telegram.uploader.network

import android.content.Context
import android.net.Uri
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.RequestBody
import okio.BufferedSink
import java.io.InputStream

class ProgressRequestBody(
    private val context: Context,
    private val uri: Uri,
    private val mimeType: String,
    private val totalBytes: Long,
    private val onProgress: (bytesUploaded: Long, totalBytes: Long) -> Unit
) : RequestBody() {

    override fun contentType(): MediaType? {
        return mimeType.toMediaTypeOrNull()
    }

    override fun contentLength(): Long {
        return totalBytes
    }

    override fun writeTo(sink: BufferedSink) {
        val inputStream: InputStream = context.contentResolver.openInputStream(uri)
            ?: throw IllegalStateException("Cannot open input stream for $uri")

        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        var uploaded: Long = 0

        inputStream.use { stream ->
            var read: Int
            while (stream.read(buffer).also { read = it } != -1) {
                sink.write(buffer, 0, read)
                uploaded += read
                onProgress(uploaded, totalBytes)
            }
        }
    }

    companion object {
        private const val DEFAULT_BUFFER_SIZE = 128 * 1024 // 128 KB buffer
    }
}
