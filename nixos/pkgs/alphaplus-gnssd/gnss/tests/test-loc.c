#define NMEA_RETRY_DELAY_MS 0
#include "../src/gnss-loc.c"
#include <glib/gstdio.h>

static GTask *pending_create;
static GTask *pending_open;
static GTask *pending_allocate;
static guint closes;
static guint releases;
static guint pause_completions;

void
qmi_device_new_from_node (QrtrNode *node, GCancellable *cancellable,
                          GAsyncReadyCallback callback, gpointer data)
{
    (void)cancellable;
    g_assert_null (pending_create);
    pending_create = g_task_new (node, NULL, callback, data);
}

QmiDevice *
qmi_device_new_from_node_finish (GAsyncResult *result, GError **error)
{
    return g_task_propagate_pointer (G_TASK (result), error);
}

void
qmi_device_open (QmiDevice *device, QmiDeviceOpenFlags flags, guint timeout,
                 GCancellable *cancellable, GAsyncReadyCallback callback, gpointer data)
{
    (void)flags; (void)timeout; (void)cancellable;
    g_assert_null (pending_open);
    pending_open = g_task_new (device, NULL, callback, data);
}

gboolean
qmi_device_open_finish (QmiDevice *device, GAsyncResult *result, GError **error)
{
    (void)device;
    return g_task_propagate_boolean (G_TASK (result), error);
}

void
qmi_device_allocate_client (QmiDevice *device, QmiService service, guint8 cid,
                            guint timeout, GCancellable *cancellable,
                            GAsyncReadyCallback callback, gpointer data)
{
    (void)service; (void)cid; (void)timeout; (void)cancellable;
    g_assert_null (pending_allocate);
    pending_allocate = g_task_new (device, NULL, callback, data);
}

QmiClient *
qmi_device_allocate_client_finish (QmiDevice *device, GAsyncResult *result, GError **error)
{
    (void)device;
    return g_task_propagate_pointer (G_TASK (result), error);
}

void
qmi_device_release_client (QmiDevice *device, QmiClient *client,
                           QmiDeviceReleaseClientFlags flags, guint timeout,
                           GCancellable *cancellable, GAsyncReadyCallback callback, gpointer data)
{
    (void)client; (void)flags; (void)timeout; (void)cancellable;
    releases++;
    GTask *task = g_task_new (device, NULL, callback, data);
    g_task_return_boolean (task, TRUE);
    g_object_unref (task);
}

gboolean
qmi_device_release_client_finish (QmiDevice *device, GAsyncResult *result, GError **error)
{
    (void)device;
    return g_task_propagate_boolean (G_TASK (result), error);
}

void
qmi_device_close_async (QmiDevice *device, guint timeout, GCancellable *cancellable,
                        GAsyncReadyCallback callback, gpointer data)
{
    (void)timeout; (void)cancellable;
    closes++;
    GTask *task = g_task_new (device, NULL, callback, data);
    g_task_return_boolean (task, TRUE);
    g_object_unref (task);
}

gboolean
qmi_device_close_finish (QmiDevice *device, GAsyncResult *result, GError **error)
{
    (void)device;
    return g_task_propagate_boolean (G_TASK (result), error);
}

gint32
qrtr_node_lookup_port (QrtrNode *node, guint32 service)
{
    (void)node;
    return service == QMI_SERVICE_LOC ? 82 : -1;
}

static GnssLoc *
new_loc (void)
{
    GnssLoc *loc = g_new0 (GnssLoc, 1);
    loc->refs = 1;
    loc->enabled = TRUE;
    loc->watched_nodes = g_ptr_array_new_with_free_func (g_object_unref);
    loc->discovery_cancellable = g_cancellable_new ();
    return loc;
}

static void
drain (void)
{
    while (g_main_context_iteration (NULL, FALSE));
}

static void
created_success (void)
{
    GTask *task = g_steal_pointer (&pending_create);
    g_assert_nonnull (task);
    g_task_return_pointer (task, g_object_new (QMI_TYPE_DEVICE, NULL), g_object_unref);
    g_object_unref (task);
    drain ();
}

static void
opened_success (void)
{
    GTask *task = g_steal_pointer (&pending_open);
    g_assert_nonnull (task);
    g_task_return_boolean (task, TRUE);
    g_object_unref (task);
    drain ();
}

static void
allocated_success (void)
{
    GTask *task = g_steal_pointer (&pending_allocate);
    g_assert_nonnull (task);
    g_task_return_pointer (task, g_object_new (QMI_TYPE_CLIENT_LOC, NULL), g_object_unref);
    g_object_unref (task);
    drain ();
}

static void
paused (GObject *source, GAsyncResult *result, gpointer data)
{
    (void)source; (void)data;
    g_autoptr(GError) error = NULL;
    g_assert_true (g_task_propagate_boolean (G_TASK (result), &error));
    g_assert_no_error (error);
    pause_completions++;
}

static void
test_pause_during_initialization (gconstpointer data)
{
    guint phase = GPOINTER_TO_UINT (data);
    closes = releases = pause_completions = 0;
    GnssLoc *loc = new_loc ();
    g_autoptr(QrtrBus) bus = g_object_new (QRTR_TYPE_BUS, NULL);
    g_autoptr(QrtrNode) node = g_object_new (QRTR_TYPE_NODE, QRTR_NODE_BUS, bus, QRTR_NODE_ID, 0, NULL);
    attach_node (loc, node);
    if (phase >= 1)
        created_success ();
    if (phase >= 2)
        opened_success ();
    gnss_loc_pause_async (loc, paused, NULL);
    drain ();
    g_assert_cmpuint (pause_completions, ==, 0);
    g_assert_cmpuint (closes, ==, 0);
    g_assert_null (loc->connection);
    if (phase == 0)
        created_success ();
    else if (phase == 1)
        opened_success ();
    else
        allocated_success ();
    drain ();
    g_assert_cmpuint (pause_completions, ==, 1);
    g_assert_cmpuint (closes, ==, 1);
    g_assert_cmpuint (releases, ==, phase == 2);
    g_assert_cmpuint (loc->closing_connections, ==, 0);
    gnss_loc_free (loc);
}

static void
test_replaced_connection (void)
{
    GnssLoc *loc = new_loc ();
    g_autoptr(QrtrBus) bus = g_object_new (QRTR_TYPE_BUS, NULL);
    g_autoptr(QrtrNode) node = g_object_new (QRTR_TYPE_NODE, QRTR_NODE_BUS, bus, QRTR_NODE_ID, 0, NULL);
    watch_node (loc, node);
    LocConnection *old = loc->connection;
    GTask *old_create = g_steal_pointer (&pending_create);
    g_signal_emit_by_name (node, "service-removed", (guint)QMI_SERVICE_LOC);
    g_assert_null (loc->connection);
    g_signal_emit_by_name (node, "service-added", (guint)QMI_SERVICE_LOC);
    LocConnection *current = loc->connection;
    g_assert_nonnull (current);
    g_assert_true (current != old);
    g_task_return_pointer (old_create, g_object_new (QMI_TYPE_DEVICE, NULL), g_object_unref);
    g_object_unref (old_create);
    drain ();
    g_assert_true (loc->connection == current);
    g_assert_true (connection_current (current));
    gnss_loc_free (loc);
    created_success ();
    drain ();
}

static gint clock_state;
static gint clock_flags;
static guint utc_sends;
static GTask *utc_request;
static guint pos_sends;
static GTask *pos_request;
static gdouble pos_last_lat;
static gdouble pos_last_lon;
static QmiLocPositionSource pos_last_source;
static guint xtra_sends;
static GTask *xtra_request;

extern QmiIndicationLocInjectUtcTimeOutput *
qmi_indication_loc_inject_utc_time_indication_parse (QmiMessage *, GError **);
extern QmiIndicationLocInjectPositionOutput *
qmi_indication_loc_inject_position_indication_parse (QmiMessage *, GError **);
extern QmiIndicationLocEngineStateOutput *
qmi_indication_loc_engine_state_indication_parse (QmiMessage *, GError **);

int
adjtimex (struct timex *clock)
{
    clock->status = clock_flags;
    clock->maxerror = 1234567;
    clock->esterror = 100;
    return clock_state;
}

void
qmi_client_loc_inject_utc_time (QmiClientLoc *client, QmiMessageLocInjectUtcTimeInput *input,
                               guint timeout, GCancellable *cancellable,
                               GAsyncReadyCallback callback, gpointer data)
{
    (void)client; (void)timeout; (void)cancellable; (void)callback;
    guint64 timestamp;
    guint32 uncertainty;
    QmiLocInjectedTimeSource source;
    g_assert_true (qmi_message_loc_inject_utc_time_input_get_utc_time (input, &timestamp, NULL));
    g_assert_true (qmi_message_loc_inject_utc_time_input_get_time_uncertainty (input, &uncertainty, NULL));
    g_assert_true (qmi_message_loc_inject_utc_time_input_get_time_source (input, &source, NULL));
    g_assert_cmpint (llabs ((gint64)timestamp - g_get_real_time () / 1000), <, 1000);
    g_assert_cmpuint (uncertainty, ==, 1236);
    g_assert_cmpint (source, ==, QMI_LOC_INJECTED_TIME_SOURCE_AP);
    utc_sends++;
    utc_request = data;
}

void
qmi_client_loc_inject_position (QmiClientLoc *client, QmiMessageLocInjectPositionInput *input,
                                guint timeout, GCancellable *cancellable,
                                GAsyncReadyCallback callback, gpointer data)
{
    (void)client; (void)timeout; (void)cancellable; (void)callback;
    gdouble lat = 0, lon = 0;
    QmiLocPositionSource source = 0;
    g_assert_true (qmi_message_loc_inject_position_input_get_latitude (input, &lat, NULL));
    g_assert_true (qmi_message_loc_inject_position_input_get_longitude (input, &lon, NULL));
    g_assert_true (qmi_message_loc_inject_position_input_get_position_source (input, &source, NULL));
    pos_last_lat = lat;
    pos_last_lon = lon;
    pos_last_source = source;
    pos_sends++;
    pos_request = data;
}

void
qmi_client_loc_inject_xtra_data (QmiClientLoc *client, QmiMessageLocInjectXtraDataInput *input,
                                guint timeout, GCancellable *cancellable,
                                GAsyncReadyCallback callback, gpointer data)
{
    (void)client; (void)input; (void)timeout; (void)cancellable; (void)callback;
    g_assert_null (xtra_request);
    xtra_sends++;
    xtra_request = data;
}

static void
complete_utc (QmiClientLoc *client)
{
    g_autoptr(QmiMessage) accepted = qmi_message_new (QMI_SERVICE_LOC, 1, 1, 0x38);
    accepted->data[6] = 0x04;
    guint32 status = GUINT32_TO_LE (QMI_LOC_INDICATION_STATUS_SUCCESS);
    g_assert_true (qmi_message_add_raw_tlv (accepted, 1, (guint8 *)&status, sizeof status, NULL));
    g_autoptr(GError) error = NULL;
    g_autoptr(QmiIndicationLocInjectUtcTimeOutput) indication =
        qmi_indication_loc_inject_utc_time_indication_parse (accepted, &error);
    g_assert_no_error (error);
    gnss_request_response (utc_request, NULL);
    g_signal_emit_by_name (client, "inject-utc-time", indication);
    g_clear_object (&utc_request);
    drain ();
}

static void
complete_position (QmiClientLoc *client)
{
    g_autoptr(QmiMessage) accepted = qmi_message_new (QMI_SERVICE_LOC, 1, 1, 0x39);
    accepted->data[6] = 0x04;
    guint32 status = GUINT32_TO_LE (QMI_LOC_INDICATION_STATUS_SUCCESS);
    g_assert_true (qmi_message_add_raw_tlv (accepted, 1, (guint8 *)&status, sizeof status, NULL));
    g_autoptr(GError) error = NULL;
    g_autoptr(QmiIndicationLocInjectPositionOutput) indication =
        qmi_indication_loc_inject_position_indication_parse (accepted, &error);
    g_assert_no_error (error);
    gnss_request_response (pos_request, NULL);
    g_signal_emit_by_name (client, "inject-position", indication);
    g_clear_object (&pos_request);
    drain ();
}

static void
test_clock_trust (void)
{
    GnssLoc *loc = new_loc ();
    LocConnection *connection = g_new0 (LocConnection, 1);
    connection->refs = 1;
    connection->owner = loc_ref (loc);
    connection->active = TRUE;
    connection->client = g_object_new (QMI_TYPE_CLIENT_LOC, NULL);
    connection->cancellable = g_cancellable_new ();
    connection->xtra = gnss_xtra_new (NULL);
    loc->connection = connection;
    utc_sends = 0;
    clock_state = TIME_OK;
    clock_flags = 0;
    connection->tracking = TRUE;
    connection->time_requested = TRUE;
    update_assistance (connection);
    g_assert_cmpuint (utc_sends, ==, 0);
    connection->engine_on = TRUE;
    connection->stop_pending = TRUE;
    connection->time_requested = TRUE;
    update_assistance (connection);
    g_assert_cmpuint (utc_sends, ==, 0);
    connection->stop_pending = FALSE;
    connection->time_requested = FALSE;
    clock_state = TIME_ERROR;
    clock_flags = 0;
    inject_time (connection);
    clock_state = TIME_OK;
    clock_flags = STA_UNSYNC;
    inject_time (connection);
    g_assert_cmpuint (utc_sends, ==, 0);
    clock_flags = 0;
    inject_time (connection);
    g_assert_cmpuint (utc_sends, ==, 1);

    complete_utc (connection->client);
    g_autoptr(GError) error = NULL;
    g_assert_true (connection->time_accepted);

    g_autoptr(QmiMessage) engine = qmi_message_new (QMI_SERVICE_LOC, 1, 1, 0x2b);
    engine->data[6] = 0x04;
    guint32 state = GUINT32_TO_LE (QMI_LOC_ENGINE_STATE_ON);
    g_assert_true (qmi_message_add_raw_tlv (engine, 1, (guint8 *)&state, sizeof state, NULL));
    g_autoptr(QmiIndicationLocEngineStateOutput) engine_output =
        qmi_indication_loc_engine_state_indication_parse (engine, &error);
    g_assert_no_error (error);
    for (guint i = 0; i < 20; i++) {
        connection->engine_on = FALSE;
        on_engine (connection->client, engine_output, connection);
    }
    g_assert_cmpuint (utc_sends, ==, 1);
    on_time_request (connection->client, NULL, connection);
    g_assert_cmpuint (utc_sends, ==, 2);
    gnss_loc_free (loc);
    gnss_request_response (utc_request, NULL);
    g_clear_object (&utc_request);
    drain ();
}

static void
test_assistance_serialization (void)
{
    g_autofree gchar *directory = g_dir_make_tmp ("gnss-assistance-XXXXXX", NULL);
    g_autofree gchar *cache = g_build_filename (directory, "xtra3grcej.bin", NULL);
    gchar payload[2048] = {0};
    g_assert_true (g_file_set_contents (cache, payload, sizeof payload, NULL));
    GnssLoc *loc = new_loc ();
    LocConnection *connection = g_new0 (LocConnection, 1);
    connection->refs = 1;
    connection->owner = loc_ref (loc);
    connection->active = TRUE;
    connection->client = g_object_new (QMI_TYPE_CLIENT_LOC, NULL);
    connection->cancellable = g_cancellable_new ();
    connection->xtra = gnss_xtra_new (directory);
    gnss_xtra_set_callback (connection->xtra, orbits_done, connection);
    g_autoptr(GArray) servers = g_array_new (FALSE, FALSE, sizeof (gchar *));
    gchar *url = "https://example.invalid/xtra.bin";
    g_array_append_val (servers, url);
    gnss_xtra_configure (connection->xtra, servers, sizeof payload, 1024);
    loc->connection = connection;
    connection->engine_on = TRUE;
    connection->tracking = TRUE;
    connection->time_requested = TRUE;
    connection->orbits_requested = TRUE;
    clock_state = TIME_OK;
    clock_flags = 0;
    utc_sends = xtra_sends = 0;
    update_assistance (connection);
    on_orbits_request (connection->client, NULL, connection);
    g_assert_cmpuint (utc_sends, ==, 1);
    g_assert_cmpuint (xtra_sends, ==, 0);
    g_assert_false (gnss_xtra_is_busy (connection->xtra));
    complete_utc (connection->client);
    g_assert_cmpuint (xtra_sends, ==, 1);
    g_assert_true (gnss_xtra_is_busy (connection->xtra));
    on_time_request (connection->client, NULL, connection);
    g_assert_cmpuint (utc_sends, ==, 1);
    g_assert_true (connection->time_requested);
    gnss_xtra_cancel (connection->xtra);
    gnss_request_response (xtra_request, NULL);
    g_clear_object (&xtra_request);
    drain ();
    g_assert_false (gnss_xtra_is_busy (connection->xtra));
    g_assert_cmpuint (utc_sends, ==, 2);
    complete_utc (connection->client);
    gnss_loc_free (loc);
    drain ();
    g_assert_cmpint (g_unlink (cache), ==, 0);
    g_assert_cmpint (g_rmdir (directory), ==, 0);
}

extern QmiIndicationLocGetPredictedOrbitsDataSourceOutput *
qmi_indication_loc_get_predicted_orbits_data_source_indication_parse (QmiMessage *, GError **);

static void
test_sources_without_optional_sizes (void)
{
    g_autoptr(QmiMessage) wire = qmi_message_new (QMI_SERVICE_LOC, 1, 1, 0x36);
    wire->data[6] = 0x04;
    guint32 status = GUINT32_TO_LE (QMI_LOC_INDICATION_STATUS_SUCCESS);
    g_assert_true (qmi_message_add_raw_tlv (wire, 1, (guint8 *)&status, sizeof status, NULL));
    const gchar *url = "https://example.invalid/xtra.bin";
    guint8 list[100] = {1, strlen (url)};
    memcpy (list + 2, url, strlen (url));
    g_assert_true (qmi_message_add_raw_tlv (wire, 0x11, list, strlen (url) + 2, NULL));
    g_autoptr(GError) error = NULL;
    g_autoptr(QmiIndicationLocGetPredictedOrbitsDataSourceOutput) output =
        qmi_indication_loc_get_predicted_orbits_data_source_indication_parse (wire, &error);
    g_assert_no_error (error);
    LocConnection connection = { .xtra = gnss_xtra_new (NULL) };
    g_assert_true (parse_sources (output, &connection, &error));
    g_assert_no_error (error);
    gnss_xtra_free (connection.xtra);
}

extern QmiIndicationLocGetNmeaTypesOutput *
qmi_indication_loc_get_nmea_types_indication_parse (QmiMessage *, GError **);
extern QmiIndicationLocSetNmeaTypesOutput *
qmi_indication_loc_set_nmea_types_indication_parse (QmiMessage *, GError **);

static guint32 initial_nmea_mask;
static guint32 accepted_nmea_mask;
static guint32 requested_nmea_mask;
static guint nmea_queries;
static guint nmea_writes;
static guint event_registrations;

static QmiMessage *
success_indication (guint16 message_id)
{
    QmiMessage *wire = qmi_message_new (QMI_SERVICE_LOC, 1, 1, message_id);
    wire->data[6] = 0x04;
    guint32 status = GUINT32_TO_LE (QMI_LOC_INDICATION_STATUS_SUCCESS);
    g_assert_true (qmi_message_add_raw_tlv (wire, 1, (guint8 *)&status, sizeof status, NULL));
    return wire;
}

void
qmi_client_loc_get_nmea_types (QmiClientLoc *client, gpointer input,
                              guint timeout, GCancellable *cancellable,
                              GAsyncReadyCallback callback, gpointer data)
{
    (void)input; (void)timeout; (void)cancellable; (void)callback;
    g_autoptr(QmiMessage) wire = success_indication (0x3f);
    guint32 mask = GUINT32_TO_LE (nmea_queries++ ? accepted_nmea_mask : initial_nmea_mask);
    g_assert_true (qmi_message_add_raw_tlv (wire, 0x10, (guint8 *)&mask, sizeof mask, NULL));
    g_autoptr(GError) error = NULL;
    g_autoptr(QmiIndicationLocGetNmeaTypesOutput) output =
        qmi_indication_loc_get_nmea_types_indication_parse (wire, &error);
    g_assert_no_error (error);
    gnss_request_response (data, NULL);
    g_signal_emit_by_name (client, "get-nmea-types", output);
    g_object_unref (data);
}

void
qmi_client_loc_set_nmea_types (QmiClientLoc *client, QmiMessageLocSetNmeaTypesInput *input,
                              guint timeout, GCancellable *cancellable,
                              GAsyncReadyCallback callback, gpointer data)
{
    (void)timeout; (void)cancellable; (void)callback;
    QmiLocNmeaType mask;
    g_assert_true (qmi_message_loc_set_nmea_types_input_get_nmea_types (input, &mask, NULL));
    requested_nmea_mask = mask;
    nmea_writes++;
    g_autoptr(QmiMessage) wire = success_indication (0x3e);
    g_autoptr(GError) error = NULL;
    g_autoptr(QmiIndicationLocSetNmeaTypesOutput) output =
        qmi_indication_loc_set_nmea_types_indication_parse (wire, &error);
    g_assert_no_error (error);
    gnss_request_response (data, NULL);
    g_signal_emit_by_name (client, "set-nmea-types", output);
    g_object_unref (data);
}

void
qmi_client_loc_register_events (QmiClientLoc *client, QmiMessageLocRegisterEventsInput *input,
                               guint timeout, GCancellable *cancellable,
                               GAsyncReadyCallback callback, gpointer data)
{
    (void)client; (void)input; (void)timeout; (void)cancellable; (void)callback;
    event_registrations++;
    connection_unref (data);
}

static LocConnection *
connected_loc (GnssLoc *loc)
{
    LocConnection *connection = g_new0 (LocConnection, 1);
    connection->refs = 1;
    connection->owner = loc_ref (loc);
    connection->active = TRUE;
    connection->client = g_object_new (QMI_TYPE_CLIENT_LOC, NULL);
    connection->cancellable = g_cancellable_new ();
    connection->xtra = gnss_xtra_new (NULL);
    loc->connection = connection;
    return connection;
}

static void
test_nmea_configuration (gconstpointer data)
{
    guint scenario = GPOINTER_TO_UINT (data);
    GnssLoc *loc = new_loc ();
    LocConnection *connection = connected_loc (loc);
    connection->step = INIT_GET_NMEA;
    nmea_queries = nmea_writes = event_registrations = requested_nmea_mask = 0;
    initial_nmea_mask = scenario == 1 ? 0x0101ffff : 0x0100ffff;
    accepted_nmea_mask = scenario == 2 ? 0x0100ffff : 0x0101ffff;
    if (scenario == 2)
        g_test_expect_message (NULL, G_LOG_LEVEL_WARNING, "*NMEA mask readback*missing required types*");
    initialize_connection (connection);
    drain ();
    guint expected_queries = scenario == 1 ? 1 : (scenario == 2 ? NMEA_RETRY_MAX + 1 : 2);
    guint expected_writes = scenario == 1 ? 0 : (scenario == 2 ? NMEA_RETRY_MAX : 1);
    g_assert_cmpuint (nmea_queries, ==, expected_queries);
    g_assert_cmpuint (nmea_writes, ==, expected_writes);
    g_assert_cmpuint (event_registrations, ==, scenario == 2 ? 0 : 1);
    if (scenario != 1)
        g_assert_cmphex (requested_nmea_mask, ==, 0x0101ffff);
    if (scenario == 2) {
        g_test_assert_expected_messages ();
        g_assert_null (loc->connection);
        g_assert_false (loc->ready);
    } else {
        g_assert_cmphex (connection->nmea_types, ==, 0x0101ffff);
    }
    gnss_loc_free (loc);
    drain ();
}

static void
test_position_assistance_and_serialization (void)
{
    g_autofree gchar *directory = g_dir_make_tmp ("gnss-pos-assist-XXXXXX", NULL);
    g_autofree gchar *xtra_cache = g_build_filename (directory, "xtra3grcej.bin", NULL);
    g_autofree gchar *pos_cache = g_build_filename (directory, "last-position.conf", NULL);
    gchar payload[2048] = {0};
    g_assert_true (g_file_set_contents (xtra_cache, payload, sizeof payload, NULL));

    GnssLoc *loc = new_loc ();
    loc->assist = gnss_assist_new (directory);
    gnss_assist_set_callback (loc->assist, assist_position_ready, loc);
    gnss_assist_record_fix (loc->assist, 31.2304, 121.4737, 15.0f,
                            (guint64)(g_get_real_time () / 1000));

    LocConnection *connection = connected_loc (loc);
    gnss_xtra_free (connection->xtra);
    connection->xtra = gnss_xtra_new (directory);
    gnss_xtra_set_callback (connection->xtra, orbits_done, connection);
    g_autoptr(GArray) servers = g_array_new (FALSE, FALSE, sizeof (gchar *));
    gchar *url = "https://example.invalid/xtra.bin";
    g_array_append_val (servers, url);
    gnss_xtra_configure (connection->xtra, servers, sizeof payload, 1024);

    connection->engine_on = TRUE;
    connection->tracking = TRUE;
    connection->time_requested = TRUE;
    connection->position_requested = TRUE;
    connection->orbits_requested = TRUE;
    clock_state = TIME_OK;
    clock_flags = 0;
    utc_sends = pos_sends = xtra_sends = 0;

    update_assistance (connection);
    g_assert_cmpuint (utc_sends, ==, 1);
    g_assert_cmpuint (pos_sends, ==, 0);
    g_assert_cmpuint (xtra_sends, ==, 0);

    complete_utc (connection->client);
    g_assert_cmpuint (pos_sends, ==, 1);
    g_assert_cmpuint (xtra_sends, ==, 0);
    g_assert_cmpfloat_with_epsilon (pos_last_lat, 31.2304, 1e-6);
    g_assert_cmpfloat_with_epsilon (pos_last_lon, 121.4737, 1e-6);
    g_assert_cmpint (pos_last_source, ==, QMI_LOC_POSITION_SOURCE_GNSS);

    complete_position (connection->client);
    g_assert_true (connection->position_accepted);
    g_assert_cmpuint (xtra_sends, ==, 1);

    gnss_xtra_cancel (connection->xtra);
    gnss_request_response (xtra_request, NULL);
    g_clear_object (&xtra_request);
    drain ();

    on_position_request (connection->client, NULL, connection);
    drain ();
    g_assert_cmpuint (pos_sends, ==, 2);
    complete_position (connection->client);

    gnss_loc_free (loc);
    drain ();
    g_assert_cmpint (g_unlink (xtra_cache), ==, 0);
    g_assert_cmpint (g_unlink (pos_cache), ==, 0);
    g_assert_cmpint (g_rmdir (directory), ==, 0);
}

int
main (int argc, char **argv)
{
    g_test_init (&argc, &argv, NULL);
    g_test_add_data_func ("/loc/pause-during-create", GUINT_TO_POINTER (0), test_pause_during_initialization);
    g_test_add_data_func ("/loc/pause-during-open", GUINT_TO_POINTER (1), test_pause_during_initialization);
    g_test_add_data_func ("/loc/pause-during-allocate", GUINT_TO_POINTER (2), test_pause_during_initialization);
    g_test_add_func ("/loc/replaced-connection", test_replaced_connection);
    g_test_add_func ("/loc/clock-trust", test_clock_trust);
    g_test_add_func ("/loc/assistance-serialization", test_assistance_serialization);
    g_test_add_func ("/loc/position-assistance-and-serialization", test_position_assistance_and_serialization);
    g_test_add_func ("/loc/sources-without-optional-sizes", test_sources_without_optional_sizes);
    g_test_add_data_func ("/loc/nmea-extended-mask", GUINT_TO_POINTER (0), test_nmea_configuration);
    g_test_add_data_func ("/loc/nmea-mask-already-configured", GUINT_TO_POINTER (1), test_nmea_configuration);
    g_test_add_data_func ("/loc/nmea-mask-readback-mismatch", GUINT_TO_POINTER (2), test_nmea_configuration);
    return g_test_run ();
}
