// SPDX-License-Identifier: GPL-2.0-or-later
#include <alsa/asoundlib.h>
#include <libmm-glib.h>

#define OPEN_RETRY_DELAY_MS 500
#define OPEN_MAX_RETRIES 10

struct call_audio {
	MMManager *manager;
	snd_pcm_t *playback;
	snd_pcm_t *capture;
	guint update;
	guint retry;
	guint retry_count;
};

static void cancel_retry(struct call_audio *audio)
{
	if (audio->retry) {
		g_source_remove(audio->retry);
		audio->retry = 0;
	}
	audio->retry_count = 0;
}

static void stop_audio(struct call_audio *audio)
{
	cancel_retry(audio);
	g_clear_pointer(&audio->capture, snd_pcm_close);
	g_clear_pointer(&audio->playback, snd_pcm_close);
}

static int open_voice_pcm(snd_pcm_t **pcm, snd_pcm_stream_t stream)
{
	int ret;

	ret = snd_pcm_open(pcm, "alphaplus_voice", stream, SND_PCM_NONBLOCK);
	if (ret < 0)
		return ret;

	return snd_pcm_set_params(*pcm, SND_PCM_FORMAT_S16_LE,
				  SND_PCM_ACCESS_RW_INTERLEAVED, 1, 8000,
				  0, 512000);
}

static gboolean has_audio_call(MMManager *manager)
{
	g_autolist(GDBusObject) modems =
		g_dbus_object_manager_get_objects(G_DBUS_OBJECT_MANAGER(manager));

	for (GList *iter = modems; iter; iter = iter->next) {
		MMModemVoice *voice = mm_object_peek_modem_voice(MM_OBJECT(iter->data));
		g_autoptr(GError) error = NULL;
		g_autolist(MMCall) calls = NULL;

		if (!voice)
			continue;

		calls = mm_modem_voice_list_calls_sync(voice, NULL, &error);
		if (error) {
			g_warning("Could not read modem calls: %s", error->message);
			continue;
		}

		for (GList *call = calls; call; call = call->next) {
			MMCallState state = mm_call_get_state(MM_CALL(call->data));

			if (state == MM_CALL_STATE_ACTIVE || state == MM_CALL_STATE_RINGING_OUT)
				return TRUE;
		}
	}

	return FALSE;
}

static gboolean update_audio(gpointer data);

static gboolean retry_audio(gpointer data)
{
	struct call_audio *audio = data;

	audio->retry = 0;
	if (!audio->update)
		audio->update = g_idle_add(update_audio, audio);
	return G_SOURCE_REMOVE;
}

static gboolean update_audio(gpointer data)
{
	struct call_audio *audio = data;
	int ret;

	audio->update = 0;
	if (!has_audio_call(audio->manager)) {
		if (audio->playback) {
			stop_audio(audio);
			g_message("Stopped the modem voice audio path");
		} else {
			cancel_retry(audio);
		}
		return G_SOURCE_REMOVE;
	}

	if (audio->playback) {
		cancel_retry(audio);
		return G_SOURCE_REMOVE;
	}

	ret = open_voice_pcm(&audio->playback, SND_PCM_STREAM_PLAYBACK);
	if (ret >= 0)
		ret = open_voice_pcm(&audio->capture, SND_PCM_STREAM_CAPTURE);
	if (ret < 0) {
		g_clear_pointer(&audio->capture, snd_pcm_close);
		g_clear_pointer(&audio->playback, snd_pcm_close);
		g_warning("Could not start the modem voice audio path: %s",
			  snd_strerror(ret));
		if (!audio->retry && audio->retry_count < OPEN_MAX_RETRIES) {
			audio->retry_count++;
			audio->retry = g_timeout_add(OPEN_RETRY_DELAY_MS, retry_audio, audio);
		}
	} else {
		cancel_retry(audio);
		g_message("Started the modem voice audio path");
	}

	return G_SOURCE_REMOVE;
}

static void queue_update(struct call_audio *audio)
{
	if (!audio->update)
		audio->update = g_idle_add(update_audio, audio);
}

static void on_call_signal(GDBusConnection *connection, const gchar *sender,
			   const gchar *path, const gchar *interface,
			   const gchar *signal, GVariant *parameters,
			   gpointer data)
{
	queue_update(data);
}

static void on_owner_changed(GObject *object, GParamSpec *property, gpointer data)
{
	queue_update(data);
}

static void on_object_changed(GDBusObjectManager *manager, GDBusObject *object, gpointer data)
{
	queue_update(data);
}

int main(void)
{
	g_autoptr(GError) error = NULL;
	g_autoptr(GDBusConnection) bus = g_bus_get_sync(G_BUS_TYPE_SYSTEM, NULL, &error);
	g_autoptr(MMManager) manager = NULL;
	g_autoptr(GMainLoop) loop = NULL;
	struct call_audio audio = {};

	if (bus)
		manager = mm_manager_new_sync(bus, G_DBUS_OBJECT_MANAGER_CLIENT_FLAGS_DO_NOT_AUTO_START,
					      NULL, &error);
	if (!manager) {
		g_printerr("Could not connect to ModemManager: %s\n", error->message);
		return EXIT_FAILURE;
	}

	audio.manager = manager;
	g_signal_connect(manager, "notify::name-owner", G_CALLBACK(on_owner_changed), &audio);
	g_signal_connect(manager, "object-added", G_CALLBACK(on_object_changed), &audio);
	g_signal_connect(manager, "object-removed", G_CALLBACK(on_object_changed), &audio);
	g_dbus_connection_signal_subscribe(bus, MM_DBUS_SERVICE, MM_DBUS_INTERFACE_CALL,
					   "StateChanged", NULL, NULL,
					   G_DBUS_SIGNAL_FLAGS_NONE, on_call_signal, &audio, NULL);
	g_dbus_connection_signal_subscribe(bus, MM_DBUS_SERVICE, MM_DBUS_INTERFACE_MODEM_VOICE,
					   NULL, NULL, NULL,
					   G_DBUS_SIGNAL_FLAGS_NONE, on_call_signal, &audio, NULL);

	queue_update(&audio);
	loop = g_main_loop_new(NULL, FALSE);
	g_main_loop_run(loop);
	return EXIT_SUCCESS;
}
