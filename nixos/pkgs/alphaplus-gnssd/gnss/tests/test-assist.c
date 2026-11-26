#include "gnss-assist.h"
#include <glib/gstdio.h>
#include <math.h>

static void
test_parse_mm_3gpp (void)
{
    const gchar *radio = NULL;
    guint mcc = 0, mnc = 0;
    guint64 area = 0, cid = 0;

    g_assert_true (gnss_assist_parse_mm_3gpp ("460,15,0,C3D2483,7848", 1u << 14,
                                              &radio, &mcc, &mnc, &area, &cid));
    g_assert_cmpstr (radio, ==, "lte");
    g_assert_cmpuint (mcc, ==, 460);
    g_assert_cmpuint (mnc, ==, 15);
    g_assert_cmpuint (area, ==, 0x7848);
    g_assert_cmpuint (cid, ==, 0xC3D2483);

    g_assert_true (gnss_assist_parse_mm_3gpp ("460,00,1A2B,3C4D", 1u << 5,
                                              &radio, &mcc, &mnc, &area, &cid));
    g_assert_cmpstr (radio, ==, "wcdma");
    g_assert_cmpuint (area, ==, 0x1A2B);
    g_assert_cmpuint (cid, ==, 0x3C4D);

    g_assert_true (gnss_assist_parse_mm_3gpp ("310,260,0,1234,ABCD", 1u << 15,
                                              &radio, &mcc, &mnc, &area, &cid));
    g_assert_cmpstr (radio, ==, "nr");
    g_assert_cmpuint (area, ==, 0xABCD);

    /* Reject zero area/cell, missing fields, or unknown access tech without TAC. */
    g_assert_false (gnss_assist_parse_mm_3gpp ("460,15,0,C3D2483", 1u << 14,
                                               &radio, &mcc, &mnc, &area, &cid));
    g_assert_false (gnss_assist_parse_mm_3gpp ("460,15,1,0", 1u << 1,
                                               &radio, &mcc, &mnc, &area, &cid));
    g_assert_false (gnss_assist_parse_mm_3gpp ("0,15,1,2", 1u << 1,
                                               &radio, &mcc, &mnc, &area, &cid));
    g_assert_false (gnss_assist_parse_mm_3gpp ("460,15,1,2", 0,
                                               &radio, &mcc, &mnc, &area, &cid));
}

static void
test_parse_beacondb_json (void)
{
    g_autoptr(GError) error = NULL;
    gdouble lat = 0, lon = 0;
    gfloat acc = 0;

    const gchar *valid = "{\"accuracy\":1500,\"location\":{\"lat\":31.2304,\"lng\":121.4737}}";
    g_assert_true (gnss_assist_parse_beacondb_json (valid, strlen (valid), &lat, &lon, &acc, &error));
    g_assert_no_error (error);
    g_assert_cmpfloat_with_epsilon (lat, 31.2304, 1e-6);
    g_assert_cmpfloat_with_epsilon (lon, 121.4737, 1e-6);
    g_assert_cmpfloat_with_epsilon (acc, 1500.0f, 1e-3);

    const gchar *lacf = "{\"accuracy\":5000,\"fallback\":\"lacf\",\"location\":{\"lat\":31.2,\"lng\":121.5}}";
    g_assert_true (gnss_assist_parse_beacondb_json (lacf, strlen (lacf), &lat, &lon, &acc, &error));
    g_assert_no_error (error);

    const gchar *ipf = "{\"accuracy\":25000,\"fallback\":\"ipf\",\"location\":{\"lat\":1.352,\"lng\":103.82}}";
    g_assert_false (gnss_assist_parse_beacondb_json (ipf, strlen (ipf), &lat, &lon, &acc, &error));
    g_assert_nonnull (error);
    g_clear_error (&error);

    const gchar *bad_lat = "{\"accuracy\":100,\"location\":{\"lat\":95.0,\"lng\":121.5}}";
    g_assert_false (gnss_assist_parse_beacondb_json (bad_lat, strlen (bad_lat), &lat, &lon, &acc, &error));
    g_assert_nonnull (error);
    g_clear_error (&error);

    const gchar *bad_types = "{\"accuracy\":\"100\",\"fallback\":null,\"location\":[]}";
    g_assert_false (gnss_assist_parse_beacondb_json (bad_types, strlen (bad_types), &lat, &lon, &acc, &error));
    g_assert_nonnull (error);
    g_clear_error (&error);

    const gchar *bad_lat_type = "{\"accuracy\":100,\"location\":{\"lat\":null,\"lng\":121.5}}";
    g_assert_false (gnss_assist_parse_beacondb_json (bad_lat_type, strlen (bad_lat_type), &lat, &lon, &acc, &error));
    g_assert_nonnull (error);
}

typedef struct {
    GMainLoop *loop;
    gboolean called;
    gboolean has_pos;
    GnssAssistPosition pos;
} Capture;

static void
on_position_ready (GnssAssist *assist, const GnssAssistPosition *pos, gpointer data)
{
    (void)assist;
    Capture *cap = data;
    cap->called = TRUE;
    cap->has_pos = pos != NULL;
    if (pos)
        cap->pos = *pos;
    g_main_loop_quit (cap->loop);
}

static void
test_cache_roundtrip_and_fresh_hit (void)
{
    g_autofree gchar *dir = g_dir_make_tmp ("gnss-assist-test-XXXXXX", NULL);
    GnssAssist *assist = gnss_assist_new (dir);
    guint64 now_ms = (guint64)(g_get_real_time () / 1000);
    gnss_assist_record_fix (assist, 31.2304, 121.4737, 12.5f, now_ms - 120000);
    gnss_assist_free (assist);

    GnssAssist *reloaded = gnss_assist_new (dir);
    g_autoptr(GMainLoop) loop = g_main_loop_new (NULL, FALSE);
    Capture cap = { .loop = loop };
    gnss_assist_set_callback (reloaded, on_position_ready, &cap);
    gnss_assist_request_position (reloaded, NULL);
    g_main_loop_run (loop);

    g_assert_true (cap.called);
    g_assert_true (cap.has_pos);
    g_assert_cmpint (cap.pos.source, ==, QMI_LOC_POSITION_SOURCE_GNSS);
    g_assert_cmpfloat_with_epsilon (cap.pos.latitude, 31.2304, 1e-6);
    g_assert_cmpfloat_with_epsilon (cap.pos.longitude, 121.4737, 1e-6);
    g_assert_cmpfloat (cap.pos.uncertainty_m, >=, 1000.0f);

    gnss_assist_free (reloaded);
    g_autofree gchar *path = g_build_filename (dir, "last-position.conf", NULL);
    g_assert_cmpint (g_unlink (path), ==, 0);
    g_assert_cmpint (g_rmdir (dir), ==, 0);
}

int
main (int argc, char **argv)
{
    g_test_init (&argc, &argv, NULL);
    g_test_add_func ("/assist/parse-mm-3gpp", test_parse_mm_3gpp);
    g_test_add_func ("/assist/parse-beacondb-json", test_parse_beacondb_json);
    g_test_add_func ("/assist/cache-roundtrip-and-fresh-hit", test_cache_roundtrip_and_fresh_hit);
    return g_test_run ();
}
