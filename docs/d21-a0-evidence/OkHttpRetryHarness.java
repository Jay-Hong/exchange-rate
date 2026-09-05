import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.mockwebserver.MockResponse;
import okhttp3.mockwebserver.MockWebServer;
import okhttp3.mockwebserver.RecordedRequest;
import okhttp3.mockwebserver.SocketPolicy;

/**
 * Runtime observations for the pinned OkHttp 4.12.0 retry/follow-up behavior.
 *
 * Runs only against MockWebServer on loopback. It does not touch an Android
 * project build directory, any repository file, or any external endpoint.
 */
public final class OkHttpRetryHarness {
    private static final MediaType JSON = MediaType.get("application/json");

    private static OkHttpClient configuredClient(boolean retryOnConnectionFailure) {
        return new OkHttpClient.Builder()
                .connectTimeout(15, TimeUnit.SECONDS)
                .readTimeout(15, TimeUnit.SECONDS)
                .writeTimeout(15, TimeUnit.SECONDS)
                .retryOnConnectionFailure(retryOnConnectionFailure)
                .build();
    }

    private static OkHttpClient currentLikeClient() {
        return configuredClient(true);
    }

    private static Request mutation(String method, String url) {
        RequestBody body = method.equals("POST") ? RequestBody.create("{}", JSON) : null;
        return new Request.Builder().url(url).method(method, body).build();
    }

    private static Map<String, Object> runStatusCase(
            String name, String method, int firstCode, String retryAfter, boolean redirect,
            boolean retryOnConnectionFailure)
            throws Exception {
        try (MockWebServer server = new MockWebServer()) {
            server.start();
            MockResponse first = new MockResponse().setResponseCode(firstCode);
            if (retryAfter != null) {
                first.setHeader("Retry-After", retryAfter);
            }
            if (redirect) {
                first.setHeader("Location", server.url("/redirected").toString());
            }
            server.enqueue(first);
            server.enqueue(new MockResponse().setResponseCode(200).setBody("ok"));

            int finalCode;
            String error = "";
            try (Response response = configuredClient(retryOnConnectionFailure).newCall(
                    mutation(method, server.url("/mutate").toString())).execute()) {
                finalCode = response.code();
            } catch (IOException exc) {
                finalCode = -1;
                error = exc.getClass().getSimpleName();
            }
            return result(name, method, finalCode, error, server);
        }
    }

    private static Map<String, Object> runSocketCase(
            String name, String method, SocketPolicy policy) throws Exception {
        try (MockWebServer server = new MockWebServer()) {
            server.start();
            server.enqueue(new MockResponse().setSocketPolicy(policy));
            server.enqueue(new MockResponse().setResponseCode(200).setBody("ok"));

            int finalCode;
            String error = "";
            try (Response response = currentLikeClient().newCall(
                    mutation(method, server.url("/mutate").toString())).execute()) {
                finalCode = response.code();
            } catch (IOException exc) {
                finalCode = -1;
                error = exc.getClass().getSimpleName();
            }
            return result(name, method, finalCode, error, server);
        }
    }

    private static Map<String, Object> result(
            String name, String method, int finalCode, String error, MockWebServer server)
            throws InterruptedException {
        List<String> requests = new ArrayList<>();
        while (true) {
            RecordedRequest request = server.takeRequest(150, TimeUnit.MILLISECONDS);
            if (request == null) {
                break;
            }
            requests.add(
                    request.getMethod() + " " + request.getPath()
                            + " bodyBytes=" + request.getBodySize());
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("name", name);
        result.put("method", method);
        result.put("finalCode", finalCode);
        result.put("error", error);
        result.put("requestCount", server.getRequestCount());
        result.put("requests", requests);
        return result;
    }

    private static String json(Map<String, Object> values) {
        StringBuilder out = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, Object> entry : values.entrySet()) {
            if (!first) out.append(',');
            first = false;
            out.append('"').append(entry.getKey()).append('"').append(':');
            Object value = entry.getValue();
            if (value instanceof Number || value instanceof Boolean) {
                out.append(value);
            } else if (value instanceof List) {
                out.append('[');
                boolean listFirst = true;
                for (Object item : (List<?>) value) {
                    if (!listFirst) out.append(',');
                    listFirst = false;
                    out.append('"').append(item).append('"');
                }
                out.append(']');
            } else {
                out.append('"').append(value).append('"');
            }
        }
        return out.append('}').toString();
    }

    public static void main(String[] args) throws Exception {
        OkHttpClient client = currentLikeClient();
        Map<String, Object> config = new LinkedHashMap<>();
        config.put("name", "client_config");
        config.put("retryOnConnectionFailure", client.retryOnConnectionFailure());
        config.put("followRedirects", client.followRedirects());
        config.put("callTimeoutMillis", client.callTimeoutMillis());
        config.put("connectTimeoutMillis", client.connectTimeoutMillis());
        config.put("readTimeoutMillis", client.readTimeoutMillis());
        config.put("writeTimeoutMillis", client.writeTimeoutMillis());
        System.out.println(json(config));

        List<Map<String, Object>> results = new ArrayList<>();
        results.add(runStatusCase("post_408_retry_after_0", "POST", 408, "0", false, true));
        results.add(runStatusCase("delete_408_retry_after_0", "DELETE", 408, "0", false, true));
        results.add(runStatusCase("post_503_retry_after_0", "POST", 503, "0", false, true));
        results.add(runStatusCase("post_503_without_retry_after", "POST", 503, null, false, true));
        results.add(runStatusCase("post_307_preserves_mutation", "POST", 307, null, true, true));
        results.add(runStatusCase("delete_308_preserves_mutation", "DELETE", 308, null, true, true));
        results.add(runStatusCase("post_401_no_authenticator", "POST", 401, null, false, true));
        results.add(runStatusCase(
                "post_408_retry_after_0_retry_disabled", "POST", 408, "0", false, false));
        results.add(runStatusCase(
                "post_503_retry_after_0_retry_disabled", "POST", 503, "0", false, false));
        results.add(runSocketCase(
                "post_disconnect_at_start", "POST", SocketPolicy.DISCONNECT_AT_START));
        results.add(runSocketCase(
                "post_disconnect_after_request", "POST", SocketPolicy.DISCONNECT_AFTER_REQUEST));
        results.add(runSocketCase(
                "delete_disconnect_after_request", "DELETE", SocketPolicy.DISCONNECT_AFTER_REQUEST));
        for (Map<String, Object> result : results) {
            System.out.println(json(result));
        }
    }
}
