#include "gnss-assist.h"
#include <glib/gstdio.h>
#include <json-glib/json-glib.h>
#include <math.h>
#include <string.h>

#define DEFAULT_CACHE_DIR "/var/lib/alphaplus-gnss"
#define POSITION_CACHE_FILENAME "last-position.conf"
#define BEACONDB_URL "https://api.beacondb.net/v1/geolocate"

#define FIX_CACHE_MAX_AGE_MS (7ULL * 24 * 3600 * 1000)
#define FIX_FRESH_AGE_MS (3600ULL * 1000)
#define FIX_SAVE_INTERVAL_MS (60ULL * 1000)
#define FIX_SAVE_DISTANCE_M 500.0
#define CELL_HIT_TTL_MS (30ULL * 60 * 1000)
#define CELL_MISS_TTL_MS (10ULL * 60 * 1000)

/* Access technology bits from ModemManager MMModemAccessTechnology. */
#define MM_TECH_GSM_MASK ((1u << 1) | (1u << 2) | (1u << 3) | (1u << 10))
#define MM_TECH_UMTS_MASK ((1u << 5) | (1u << 6) | (1u << 7) | (1u << 8) | (1u << 9))
#define MM_TECH_LTE_MASK ((1u << 14) | (1u << 16) | (1u << 17))
#define MM_TECH_NR_MASK (1u << 15)
#define MM_LOC_SOURCE_3GPP_LAC_CI (1u << 0)

typedef struct _AssistOperation AssistOperation;

struct _GnssAssist {
    guint refs;
    gchar *cache_dir;
    gchar *cache_path;

    gboolean has_fix;
    gdouble fix_lat;
    gdouble fix_lon;
    gfloat fix_uncertainty;
    guint64 fix_timestamp_ms;
    gboolean fix_dirty;
    guint64 last_save_ms;

    gchar *cell_key;
    guint64 cell_lookup_ms;
    gboolean cell_hit;
    GnssAssistPosition cell_pos;

    AssistOperation *operation;
    GnssAssistPositionCb callback;
    gpointer user_data;
};

struct _AssistOperation {
    guint refs;
    GnssAssist *owner;
    gboolean active;
    GCancellable *cancellable;
    GCancellable *parent;
    gulong parent_cancelled_id;
    GDBusConnection *dbus;
    GSubprocess *process;
    guint idle_id;

    gchar *modem_path;
    guint32 enabled_sources;
    gboolean signals_location;
    guint32 access_tech;
    gchar *cell_key;
    gchar *payload;
};

static GnssAssist *
assist_ref (GnssAssist *assist)
{
    assist->refs++;
    return assist;
}

static void
assist_unref (GnssAssist *assist)
{
    if (--assist->refs)
        return;
    g_free (assist->cache_dir);
    g_free (assist->cache_path);
    g_free (assist->cell_key);
    g_free (assist);
}

static AssistOperation *
operation_ref (AssistOperation *operation)
{
    operation->refs++;
    return operation;
}

static void
operation_unref (AssistOperation *operation)
{
    if (--operation->refs)
        return;
    if (operation->parent_cancelled_id)
        g_signal_handler_disconnect (operation->parent, operation->parent_cancelled_id);
    if (operation->idle_id)
        g_source_remove (operation->idle_id);
    g_clear_object (&operation->process);
    g_clear_object (&operation->dbus);
    g_clear_object (&operation->parent);
    g_clear_object (&operation->cancellable);
    g_free (operation->modem_path);
    g_free (operation->cell_key);
    g_free (operation->payload);
    assist_unref (operation->owner);
    g_free (operation);
}

static void
parent_cancelled (GCancellable *parent, gpointer data)
{
    (void)parent;
    AssistOperation *operation = data;
    g_cancellable_cancel (operation->cancellable);
    if (operation->process)
        g_subprocess_force_exit (operation->process);
}

static gboolean
valid_coords (gdouble lat, gdouble lon, gfloat uncertainty)
{
    return isfinite (lat) && isfinite (lon) && isfinite (uncertainty) &&
           lat >= -90.0 && lat <= 90.0 && lon >= -180.0 && lon <= 180.0 &&
           uncertainty >= 0.0f;
}

static gdouble
approx_distance_m (gdouble lat1, gdouble lon1, gdouble lat2, gdouble lon2)
{
    const gdouble deg_to_rad = G_PI / 180.0;
    const gdouble earth_r = 6371000.0;
    gdouble dlat = (lat2 - lat1) * deg_to_rad;
    gdouble dlon = (lon2 - lon1) * deg_to_rad * cos (0.5 * (lat1 + lat2) * deg_to_rad);
    return earth_r * sqrt (dlat * dlat + dlon * dlon);
}

static void
load_cache (GnssAssist *assist)
{
    g_autoptr(GKeyFile) keyfile = g_key_file_new ();
    if (!g_key_file_load_from_file (keyfile, assist->cache_path, G_KEY_FILE_NONE, NULL))
        return;

    g_autoptr(GError) error = NULL;
    gdouble lat = g_key_file_get_double (keyfile, "position", "latitude", &error);
    if (error)
        return;
    gdouble lon = g_key_file_get_double (keyfile, "position", "longitude", &error);
    if (error)
        return;
    gdouble unc = g_key_file_get_double (keyfile, "position", "uncertainty_m", &error);
    if (error)
        return;
    guint64 ts = g_key_file_get_uint64 (keyfile, "position", "timestamp_ms", &error);
    if (error || !ts || !valid_coords (lat, lon, (gfloat)unc))
        return;

    assist->has_fix = TRUE;
    assist->fix_lat = lat;
    assist->fix_lon = lon;
    assist->fix_uncertainty = (gfloat)MAX (unc, 1.0);
    assist->fix_timestamp_ms = ts;
    assist->last_save_ms = ts;
}

static gboolean
get_cached_position (GnssAssist *assist, guint64 max_age_ms, GnssAssistPosition *out)
{
    if (!assist->has_fix)
        return FALSE;

    guint64 now_ms = (guint64)(g_get_real_time () / 1000);
    guint64 age_ms = now_ms > assist->fix_timestamp_ms ? now_ms - assist->fix_timestamp_ms : 0;
    if (age_ms > max_age_ms)
        return FALSE;

    gdouble scaled = fmax ((gdouble)assist->fix_uncertainty, 1000.0) +
                     ((gdouble)age_ms / 3600000.0) * 5000.0;
    if (scaled > 500000.0)
        scaled = 500000.0;

    out->latitude = assist->fix_lat;
    out->longitude = assist->fix_lon;
    out->uncertainty_m = (gfloat)scaled;
    out->timestamp_ms = assist->fix_timestamp_ms;
    out->source = QMI_LOC_POSITION_SOURCE_GNSS;
    return TRUE;
}

static void
finish_operation (AssistOperation *operation, const GnssAssistPosition *position)
{
    if (!operation->active)
        return;
    GnssAssist *assist = operation->owner;
    operation->active = FALSE;
    assist->operation = NULL;
    if (assist->callback && !g_cancellable_is_cancelled (operation->cancellable))
        assist->callback (assist, position, assist->user_data);
    operation_unref (operation);
}

static void
finish_with_cached_fallback (AssistOperation *operation)
{
    GnssAssistPosition pos;
    if (get_cached_position (operation->owner, FIX_CACHE_MAX_AGE_MS, &pos))
        finish_operation (operation, &pos);
    else
        finish_operation (operation, NULL);
}

gboolean
gnss_assist_parse_mm_3gpp (const gchar *raw_3gpp, guint32 access_tech,
                           const gchar **radio_type, guint *mcc, guint *mnc,
                           guint64 *lac_or_tac, guint64 *cell_id)
{
    if (!raw_3gpp || !*raw_3gpp)
        return FALSE;

    g_auto(GStrv) parts = g_strsplit (raw_3gpp, ",", -1);
    guint len = g_strv_length (parts);
    if (len < 4)
        return FALSE;

    gchar *end = NULL;
    guint64 parsed_mcc = g_ascii_strtoull (parts[0], &end, 10);
    if (!parts[0][0] || *end != '\0' || parsed_mcc == 0 || parsed_mcc > 999)
        return FALSE;

    guint64 parsed_mnc = g_ascii_strtoull (parts[1], &end, 10);
    if (!parts[1][0] || *end != '\0' || parsed_mnc > 999)
        return FALSE;

    guint64 parsed_lac = g_ascii_strtoull (parts[2], &end, 16);
    if (!parts[2][0] || *end != '\0')
        return FALSE;

    guint64 parsed_cid = g_ascii_strtoull (parts[3], &end, 16);
    if (!parts[3][0] || *end != '\0' || parsed_cid == 0)
        return FALSE;

    guint64 parsed_tac = 0;
    if (len >= 5 && parts[4][0]) {
        parsed_tac = g_ascii_strtoull (parts[4], &end, 16);
        if (*end != '\0')
            return FALSE;
    }

    const gchar *type = NULL;
    guint64 area = parsed_lac;
    if (access_tech & MM_TECH_NR_MASK) {
        type = "nr";
        if (parsed_tac)
            area = parsed_tac;
    } else if (access_tech & MM_TECH_LTE_MASK) {
        type = "lte";
        if (parsed_tac)
            area = parsed_tac;
    } else if (access_tech & MM_TECH_UMTS_MASK) {
        type = "wcdma";
    } else if (access_tech & MM_TECH_GSM_MASK) {
        type = "gsm";
    } else if (parsed_tac) {
        type = "lte";
        area = parsed_tac;
    } else {
        return FALSE;
    }

    if (area == 0)
        return FALSE;

    if (radio_type)
        *radio_type = type;
    if (mcc)
        *mcc = (guint)parsed_mcc;
    if (mnc)
        *mnc = (guint)parsed_mnc;
    if (lac_or_tac)
        *lac_or_tac = area;
    if (cell_id)
        *cell_id = parsed_cid;
    return TRUE;
}

static gboolean
get_json_double_member (JsonObject *obj, const gchar *member, gdouble *out)
{
    JsonNode *node = json_object_get_member (obj, member);
    if (!node || !JSON_NODE_HOLDS_VALUE (node) || json_node_is_null (node))
        return FALSE;

    GType type = json_node_get_value_type (node);
    if (type != G_TYPE_DOUBLE && type != G_TYPE_INT64)
        return FALSE;

    *out = json_node_get_double (node);
    return TRUE;
}

gboolean
gnss_assist_parse_beacondb_json (const gchar *json_text, gsize json_len,
                                 gdouble *latitude, gdouble *longitude,
                                 gfloat *accuracy_m, GError **error)
{
    g_autoptr(JsonParser) parser = json_parser_new ();
    if (!json_parser_load_from_data (parser, json_text, (gssize)json_len, error))
        return FALSE;

    JsonNode *root = json_parser_get_root (parser);
    if (!root || !JSON_NODE_HOLDS_OBJECT (root)) {
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                             "BeaconDB response is not a JSON object");
        return FALSE;
    }

    JsonObject *obj = json_node_get_object (root);
    JsonNode *fallback_node = json_object_get_member (obj, "fallback");
    if (fallback_node) {
        if (!JSON_NODE_HOLDS_VALUE (fallback_node) ||
            json_node_get_value_type (fallback_node) != G_TYPE_STRING) {
            g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                                 "BeaconDB fallback field is not a string");
            return FALSE;
        }
        if (g_strcmp0 (json_node_get_string (fallback_node), "ipf") == 0) {
            g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                                 "Rejected IP geolocation fallback");
            return FALSE;
        }
    }

    JsonNode *loc_node = json_object_get_member (obj, "location");
    if (!loc_node || !JSON_NODE_HOLDS_OBJECT (loc_node)) {
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                             "BeaconDB response missing location object");
        return FALSE;
    }

    JsonObject *loc = json_node_get_object (loc_node);
    gdouble lat = 0.0, lon = 0.0, acc = 0.0;
    if (!get_json_double_member (loc, "lat", &lat) ||
        !get_json_double_member (loc, "lng", &lon) ||
        !get_json_double_member (obj, "accuracy", &acc)) {
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                             "BeaconDB location missing numeric coordinates or accuracy");
        return FALSE;
    }

    if (!valid_coords (lat, lon, (gfloat)acc) || acc <= 0.0) {
        g_set_error_literal (error, G_IO_ERROR, G_IO_ERROR_INVALID_DATA,
                             "BeaconDB returned out-of-range coordinates");
        return FALSE;
    }

    if (latitude)
        *latitude = lat;
    if (longitude)
        *longitude = lon;
    if (accuracy_m)
        *accuracy_m = (gfloat)acc;
    return TRUE;
}

static void
cache_cell_result (GnssAssist *assist, const gchar *key, gboolean hit,
                   const GnssAssistPosition *pos)
{
    g_free (assist->cell_key);
    assist->cell_key = g_strdup (key);
    assist->cell_lookup_ms = (guint64)(g_get_real_time () / 1000);
    assist->cell_hit = hit;
    if (hit && pos)
        assist->cell_pos = *pos;
}

static void
beacondb_done (GObject *source, GAsyncResult *result, gpointer data)
{
    GSubprocess *proc = G_SUBPROCESS (source);
    AssistOperation *operation = data;
    g_autoptr(GError) error = NULL;
    g_autofree gchar *stdout_buf = NULL;
    g_autofree gchar *stderr_buf = NULL;
    gboolean ok = g_subprocess_communicate_utf8_finish (
        proc, result, &stdout_buf, &stderr_buf, &error);
    g_clear_object (&operation->process);

    if (!operation->active || g_cancellable_is_cancelled (operation->cancellable)) {
        finish_operation (operation, NULL);
        operation_unref (operation);
        return;
    }

    gboolean should_cache_miss = FALSE;
    if (ok && g_subprocess_get_successful (proc)) {
        if (stdout_buf && *stdout_buf) {
            gdouble lat = 0, lon = 0;
            gfloat acc = 0;
            if (gnss_assist_parse_beacondb_json (stdout_buf, strlen (stdout_buf),
                                                 &lat, &lon, &acc, &error)) {
                GnssAssistPosition pos = {
                    .latitude = lat,
                    .longitude = lon,
                    .uncertainty_m = MAX (acc, 1000.0f),
                    .timestamp_ms = (guint64)(g_get_real_time () / 1000),
                    .source = QMI_LOC_POSITION_SOURCE_CELL_ID,
                };
                cache_cell_result (operation->owner, operation->cell_key, TRUE, &pos);
                g_message ("[ASSIST] Cell lookup resolved %.4f, %.4f (~%.0fm)",
                           pos.latitude, pos.longitude, pos.uncertainty_m);
                finish_operation (operation, &pos);
                operation_unref (operation);
                return;
            }
            g_debug ("[ASSIST] Cell lookup unusable: %s", error->message);
        } else {
            g_debug ("[ASSIST] Cell lookup returned empty response");
        }
        should_cache_miss = TRUE;
    } else if (ok) {
        gboolean exited = g_subprocess_get_if_exited (proc);
        gint exit_code = exited ? g_subprocess_get_exit_status (proc) : -1;
        g_debug ("[ASSIST] Cell lookup curl exited with %d%s%s", exit_code,
                 stderr_buf && *stderr_buf ? " - " : "",
                 stderr_buf ? stderr_buf : "");
        /* Cache definitive HTTP errors (curl --fail exit 22), not transient network failures. */
        if (exited && exit_code == 22)
            should_cache_miss = TRUE;
    } else if (error) {
        g_debug ("[ASSIST] Cell lookup failed: %s%s%s", error->message,
                 stderr_buf && *stderr_buf ? " - " : "",
                 stderr_buf ? stderr_buf : "");
    }

    if (should_cache_miss)
        cache_cell_result (operation->owner, operation->cell_key, FALSE, NULL);
    finish_with_cached_fallback (operation);
    operation_unref (operation);
}

static void
query_beacondb_cell (AssistOperation *operation, const gchar *radio_type,
                     guint mcc, guint mnc, guint64 area, guint64 cell_id)
{
    GnssAssist *assist = operation->owner;
    operation->cell_key = g_strdup_printf ("%s:%u:%u:%" G_GUINT64_FORMAT ":%" G_GUINT64_FORMAT,
                                           radio_type, mcc, mnc, area, cell_id);

    guint64 now_ms = (guint64)(g_get_real_time () / 1000);
    if (g_strcmp0 (assist->cell_key, operation->cell_key) == 0) {
        guint64 age_ms = now_ms > assist->cell_lookup_ms ? now_ms - assist->cell_lookup_ms : 0;
        guint64 ttl = assist->cell_hit ? CELL_HIT_TTL_MS : CELL_MISS_TTL_MS;
        if (age_ms < ttl) {
            if (assist->cell_hit) {
                GnssAssistPosition pos = assist->cell_pos;
                pos.timestamp_ms = now_ms;
                finish_operation (operation, &pos);
            } else {
                finish_with_cached_fallback (operation);
            }
            return;
        }
    }

    operation->payload = g_strdup_printf (
        "{\"fallbacks\":{\"lacf\":true,\"ipf\":false},"
        "\"cellTowers\":[{\"radioType\":\"%s\",\"mobileCountryCode\":%u,"
        "\"mobileNetworkCode\":%u,\"locationAreaCode\":%" G_GUINT64_FORMAT ","
        "\"cellId\":%" G_GUINT64_FORMAT "}]}",
        radio_type, mcc, mnc, area, cell_id);

    g_autoptr(GError) error = NULL;
    operation->process = g_subprocess_new (
        G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_PIPE, &error,
        "/usr/bin/curl",
        "--silent", "--show-error", "--fail", "--proto", "=https",
        "--user-agent", "alphaplus-gnssd/" DAEMON_VERSION " (Linux; aarch64; LG-G820N)",
        "--connect-timeout", "5", "--max-time", "10", "--max-filesize", "65536",
        "-H", "Content-Type: application/json",
        "--data-raw", operation->payload,
        "--url", BEACONDB_URL, NULL);
    if (!operation->process) {
        g_debug ("[ASSIST] Failed to spawn curl: %s", error->message);
        finish_with_cached_fallback (operation);
        return;
    }

    g_subprocess_communicate_utf8_async (operation->process, NULL, operation->cancellable,
                                         beacondb_done, operation_ref (operation));
}

static void
handle_mm_location_dict (AssistOperation *operation, GVariant *loc_dict)
{
    g_autoptr(GVariant) v3gpp = NULL;
    GVariantIter iter;
    guint32 key = 0;
    GVariant *val = NULL;
    g_variant_iter_init (&iter, loc_dict);
    while (g_variant_iter_loop (&iter, "{uv}", &key, &val)) {
        if (key == MM_LOC_SOURCE_3GPP_LAC_CI && g_variant_is_of_type (val, G_VARIANT_TYPE_STRING)) {
            v3gpp = g_steal_pointer (&val);
            break;
        }
    }

    const gchar *radio_type = NULL;
    guint mcc = 0, mnc = 0;
    guint64 area = 0, cell_id = 0;
    if (v3gpp && gnss_assist_parse_mm_3gpp (g_variant_get_string (v3gpp, NULL),
                                            operation->access_tech, &radio_type,
                                            &mcc, &mnc, &area, &cell_id)) {
        query_beacondb_cell (operation, radio_type, mcc, mnc, area, cell_id);
        return;
    }

    finish_with_cached_fallback (operation);
}

static void
mm_get_location_done (GObject *source, GAsyncResult *result, gpointer data)
{
    AssistOperation *operation = data;
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_connection_call_finish (
        G_DBUS_CONNECTION (source), result, &error);

    if (!operation->active || g_cancellable_is_cancelled (operation->cancellable)) {
        finish_operation (operation, NULL);
        operation_unref (operation);
        return;
    }

    if (!reply) {
        g_debug ("[ASSIST] ModemManager GetLocation failed: %s", error->message);
        finish_with_cached_fallback (operation);
        operation_unref (operation);
        return;
    }

    g_autoptr(GVariant) loc_dict = g_variant_get_child_value (reply, 0);
    handle_mm_location_dict (operation, loc_dict);
    operation_unref (operation);
}

static void
mm_setup_done (GObject *source, GAsyncResult *result, gpointer data)
{
    AssistOperation *operation = data;
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_connection_call_finish (
        G_DBUS_CONNECTION (source), result, &error);

    if (!operation->active || g_cancellable_is_cancelled (operation->cancellable)) {
        finish_operation (operation, NULL);
        operation_unref (operation);
        return;
    }

    if (!reply) {
        g_debug ("[ASSIST] ModemManager Location.Setup failed: %s", error->message);
        finish_with_cached_fallback (operation);
        operation_unref (operation);
        return;
    }

    g_dbus_connection_call (
        operation->dbus, "org.freedesktop.ModemManager1", operation->modem_path,
        "org.freedesktop.ModemManager1.Modem.Location", "GetLocation", NULL,
        G_VARIANT_TYPE ("(a{uv})"), G_DBUS_CALL_FLAGS_NONE, 2000,
        operation->cancellable, mm_get_location_done, operation_ref (operation));
    operation_unref (operation);
}

static void
mm_objects_done (GObject *source, GAsyncResult *result, gpointer data)
{
    AssistOperation *operation = data;
    g_autoptr(GError) error = NULL;
    g_autoptr(GVariant) reply = g_dbus_connection_call_finish (
        G_DBUS_CONNECTION (source), result, &error);

    if (!operation->active || g_cancellable_is_cancelled (operation->cancellable)) {
        finish_operation (operation, NULL);
        operation_unref (operation);
        return;
    }

    if (!reply) {
        g_debug ("[ASSIST] ModemManager enumeration failed: %s", error->message);
        finish_with_cached_fallback (operation);
        operation_unref (operation);
        return;
    }

    g_autoptr(GVariant) objects = g_variant_get_child_value (reply, 0);
    GVariantIter obj_iter;
    const gchar *obj_path = NULL;
    GVariant *ifaces = NULL;
    gboolean found = FALSE;
    guint32 caps = 0;

    g_variant_iter_init (&obj_iter, objects);
    while (g_variant_iter_loop (&obj_iter, "{&o@a{sa{sv}}}", &obj_path, &ifaces)) {
        g_autoptr(GVariant) modem_props = g_variant_lookup_value (
            ifaces, "org.freedesktop.ModemManager1.Modem", G_VARIANT_TYPE_VARDICT);
        g_autoptr(GVariant) loc_props = g_variant_lookup_value (
            ifaces, "org.freedesktop.ModemManager1.Modem.Location", G_VARIANT_TYPE_VARDICT);
        if (!modem_props || !loc_props)
            continue;

        caps = 0;
        g_variant_lookup (loc_props, "Capabilities", "u", &caps);
        if (!(caps & MM_LOC_SOURCE_3GPP_LAC_CI))
            continue;

        operation->modem_path = g_strdup (obj_path);
        g_variant_lookup (modem_props, "AccessTechnologies", "u", &operation->access_tech);
        g_variant_lookup (loc_props, "Enabled", "u", &operation->enabled_sources);
        g_variant_lookup (loc_props, "SignalsLocation", "b", &operation->signals_location);
        found = TRUE;
        g_variant_unref (ifaces);
        break;
    }

    if (!found) {
        finish_with_cached_fallback (operation);
        operation_unref (operation);
        return;
    }

    if (!(operation->enabled_sources & MM_LOC_SOURCE_3GPP_LAC_CI)) {
        g_dbus_connection_call (
            operation->dbus, "org.freedesktop.ModemManager1", operation->modem_path,
            "org.freedesktop.ModemManager1.Modem.Location", "Setup",
            g_variant_new ("(ub)", operation->enabled_sources | MM_LOC_SOURCE_3GPP_LAC_CI,
                           operation->signals_location),
            NULL, G_DBUS_CALL_FLAGS_NONE, 2000,
            operation->cancellable, mm_setup_done, operation_ref (operation));
    } else {
        g_dbus_connection_call (
            operation->dbus, "org.freedesktop.ModemManager1", operation->modem_path,
            "org.freedesktop.ModemManager1.Modem.Location", "GetLocation", NULL,
            G_VARIANT_TYPE ("(a{uv})"), G_DBUS_CALL_FLAGS_NONE, 2000,
            operation->cancellable, mm_get_location_done, operation_ref (operation));
    }
    operation_unref (operation);
}

static void
dbus_ready (GObject *source, GAsyncResult *result, gpointer data)
{
    (void)source;
    AssistOperation *operation = data;
    g_autoptr(GError) error = NULL;
    operation->dbus = g_bus_get_finish (result, &error);

    if (!operation->active || g_cancellable_is_cancelled (operation->cancellable)) {
        finish_operation (operation, NULL);
        operation_unref (operation);
        return;
    }

    if (!operation->dbus) {
        g_debug ("[ASSIST] System D-Bus unavailable: %s", error->message);
        finish_with_cached_fallback (operation);
        operation_unref (operation);
        return;
    }

    g_dbus_connection_call (
        operation->dbus, "org.freedesktop.ModemManager1", "/org/freedesktop/ModemManager1",
        "org.freedesktop.DBus.ObjectManager", "GetManagedObjects", NULL,
        G_VARIANT_TYPE ("(a{oa{sa{sv}}})"), G_DBUS_CALL_FLAGS_NO_AUTO_START, 2000,
        operation->cancellable, mm_objects_done, operation_ref (operation));
    operation_unref (operation);
}

static gboolean
deliver_fresh_cache (gpointer data)
{
    AssistOperation *operation = data;
    operation->idle_id = 0;
    GnssAssistPosition pos;
    if (get_cached_position (operation->owner, FIX_FRESH_AGE_MS, &pos))
        finish_operation (operation, &pos);
    else
        finish_with_cached_fallback (operation);
    operation_unref (operation);
    return G_SOURCE_REMOVE;
}

GnssAssist *
gnss_assist_new (const gchar *cache_dir)
{
    GnssAssist *assist = g_new0 (GnssAssist, 1);
    assist->refs = 1;
    assist->cache_dir = g_strdup (cache_dir ? cache_dir : DEFAULT_CACHE_DIR);
    assist->cache_path = g_build_filename (assist->cache_dir, POSITION_CACHE_FILENAME, NULL);
    load_cache (assist);
    return assist;
}

void
gnss_assist_set_callback (GnssAssist *assist, GnssAssistPositionCb callback, gpointer data)
{
    assist->callback = callback;
    assist->user_data = data;
}

void
gnss_assist_record_fix (GnssAssist *assist, gdouble latitude, gdouble longitude,
                        gfloat uncertainty_m, guint64 timestamp_ms)
{
    if (!valid_coords (latitude, longitude, uncertainty_m) || !timestamp_ms)
        return;

    gboolean moved = !assist->has_fix ||
                     approx_distance_m (assist->fix_lat, assist->fix_lon,
                                        latitude, longitude) >= FIX_SAVE_DISTANCE_M;
    assist->has_fix = TRUE;
    assist->fix_lat = latitude;
    assist->fix_lon = longitude;
    assist->fix_uncertainty = MAX (uncertainty_m, 1.0f);
    assist->fix_timestamp_ms = timestamp_ms;
    assist->fix_dirty = TRUE;

    guint64 now_ms = (guint64)(g_get_real_time () / 1000);
    if (!assist->last_save_ms || moved ||
        (now_ms > assist->last_save_ms && now_ms - assist->last_save_ms >= FIX_SAVE_INTERVAL_MS))
        gnss_assist_flush_cache (assist);
}

void
gnss_assist_flush_cache (GnssAssist *assist)
{
    if (!assist->has_fix || !assist->fix_dirty)
        return;
    if (g_mkdir_with_parents (assist->cache_dir, 0750))
        return;

    g_autoptr(GKeyFile) keyfile = g_key_file_new ();
    g_key_file_set_double (keyfile, "position", "latitude", assist->fix_lat);
    g_key_file_set_double (keyfile, "position", "longitude", assist->fix_lon);
    g_key_file_set_double (keyfile, "position", "uncertainty_m", assist->fix_uncertainty);
    g_key_file_set_uint64 (keyfile, "position", "timestamp_ms", assist->fix_timestamp_ms);

    gsize len = 0;
    g_autofree gchar *data = g_key_file_to_data (keyfile, &len, NULL);
    if (data && g_file_set_contents (assist->cache_path, data, (gssize)len, NULL)) {
        assist->fix_dirty = FALSE;
        assist->last_save_ms = (guint64)(g_get_real_time () / 1000);
    }
}

gboolean
gnss_assist_request_position (GnssAssist *assist, GCancellable *cancellable)
{
    if (assist->operation)
        return FALSE;
    if (cancellable && g_cancellable_is_cancelled (cancellable))
        return FALSE;

    AssistOperation *operation = g_new0 (AssistOperation, 1);
    operation->refs = 1;
    operation->owner = assist_ref (assist);
    operation->active = TRUE;
    operation->cancellable = g_cancellable_new ();
    if (cancellable) {
        operation->parent = g_object_ref (cancellable);
        operation->parent_cancelled_id = g_signal_connect (
            cancellable, "cancelled", G_CALLBACK (parent_cancelled), operation);
    }
    assist->operation = operation;

    GnssAssistPosition pos;
    if (get_cached_position (assist, FIX_FRESH_AGE_MS, &pos)) {
        operation->idle_id = g_idle_add (deliver_fresh_cache, operation_ref (operation));
        return TRUE;
    }

    g_bus_get (G_BUS_TYPE_SYSTEM, operation->cancellable, dbus_ready, operation_ref (operation));
    return TRUE;
}

gboolean
gnss_assist_is_busy (GnssAssist *assist)
{
    return assist->operation != NULL;
}

void
gnss_assist_cancel (GnssAssist *assist)
{
    AssistOperation *operation = assist->operation;
    if (!operation)
        return;
    operation->active = FALSE;
    assist->operation = NULL;
    g_cancellable_cancel (operation->cancellable);
    if (operation->process)
        g_subprocess_force_exit (operation->process);
    if (operation->idle_id) {
        g_source_remove (operation->idle_id);
        operation->idle_id = 0;
        operation_unref (operation);
    }
    operation_unref (operation);
}

void
gnss_assist_free (GnssAssist *assist)
{
    assist->callback = NULL;
    assist->user_data = NULL;
    gnss_assist_cancel (assist);
    gnss_assist_flush_cache (assist);
    assist_unref (assist);
}
