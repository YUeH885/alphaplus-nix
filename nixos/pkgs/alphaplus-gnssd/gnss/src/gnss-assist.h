#pragma once
#include "gnss-common.h"

typedef struct _GnssAssist GnssAssist;

typedef struct {
    gdouble latitude;
    gdouble longitude;
    gfloat uncertainty_m;
    guint64 timestamp_ms;
    QmiLocPositionSource source;
} GnssAssistPosition;

typedef void (*GnssAssistPositionCb) (GnssAssist *assist,
                                      const GnssAssistPosition *position,
                                      gpointer data);

GnssAssist *gnss_assist_new (const gchar *cache_dir);
void gnss_assist_set_callback (GnssAssist *assist, GnssAssistPositionCb callback, gpointer data);
void gnss_assist_record_fix (GnssAssist *assist, gdouble latitude, gdouble longitude,
                             gfloat uncertainty_m, guint64 timestamp_ms);
void gnss_assist_flush_cache (GnssAssist *assist);
gboolean gnss_assist_request_position (GnssAssist *assist, GCancellable *cancellable);
gboolean gnss_assist_is_busy (GnssAssist *assist);
void gnss_assist_cancel (GnssAssist *assist);
void gnss_assist_free (GnssAssist *assist);

gboolean gnss_assist_parse_mm_3gpp (const gchar *raw_3gpp, guint32 access_tech,
                                    const gchar **radio_type, guint *mcc, guint *mnc,
                                    guint64 *lac_or_tac, guint64 *cell_id);
gboolean gnss_assist_parse_beacondb_json (const gchar *json_text, gsize json_len,
                                          gdouble *latitude, gdouble *longitude,
                                          gfloat *accuracy_m, GError **error);
