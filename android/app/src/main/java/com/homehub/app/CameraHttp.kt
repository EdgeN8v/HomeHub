package com.homehub.app

import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Request
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume

internal object CameraHttp {
    private val client by lazy { Net.client.newBuilder().callTimeout(8, TimeUnit.SECONDS).build() }
    suspend fun json(request: Request): JSONObject? = suspendCancellableCoroutine { cont ->
        val call = client.newCall(request)
        cont.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) { if (cont.isActive) cont.resume(null) }
            override fun onResponse(call: Call, response: Response) {
                val value = response.use { r ->
                    if (r.isSuccessful) runCatching { JSONObject(r.body?.string() ?: "") }.getOrNull() else null
                }
                if (cont.isActive) cont.resume(value)
            }
        })
    }
}
