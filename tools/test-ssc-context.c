/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <math.h>
#include <libssc-sensor-accelerometer.h>

typedef struct {
	GMainContext *context;
	GMainLoop *loop;
	GThread *thread;
	guint count;
	gfloat last[3];
} Capture;

static gboolean unexpected_default_dispatch(gpointer data)
{
	(void)data;
	g_error("Sensor worker dispatched the global default context");
	return G_SOURCE_REMOVE;
}

static void measurement(SSCSensorAccelerometer *sensor, gfloat x, gfloat y,
			gfloat z, gpointer data)
{
	Capture *capture = data;

	(void)sensor;
	g_assert_true(g_thread_self() == capture->thread);
	g_assert_true(g_main_context_is_owner(capture->context));
	g_assert_true(isfinite(x) && isfinite(y) && isfinite(z));
	capture->count++;
	capture->last[0] = x;
	capture->last[1] = y;
	capture->last[2] = z;
}

static gboolean finish_capture(gpointer data)
{
	g_main_loop_quit(((Capture *)data)->loop);
	return G_SOURCE_REMOVE;
}

static gpointer worker(gpointer data)
{
	Capture capture = { .context = data, .thread = g_thread_self() };

	g_main_context_push_thread_default(capture.context);
	capture.loop = g_main_loop_new(capture.context, FALSE);
	for (guint cycle = 1; cycle <= 2; cycle++) {
		g_autoptr(GError) error = NULL;
		SSCSensorAccelerometer *sensor =
			ssc_sensor_accelerometer_new_sync(NULL, &error);
		g_assert_no_error(error);
		g_assert_nonnull(sensor);
		capture.count = 0;
		gulong handler = g_signal_connect(sensor, "measurement",
						 G_CALLBACK(measurement), &capture);
		g_assert_true(ssc_sensor_accelerometer_open_sync(sensor, NULL, &error));
		g_assert_no_error(error);
		GSource *timeout = g_timeout_source_new(1000);
		g_source_set_callback(timeout, finish_capture, &capture, NULL);
		g_source_attach(timeout, capture.context);
		g_main_loop_run(capture.loop);
		g_source_unref(timeout);
		g_assert_cmpuint(capture.count, >=, 10);
		g_signal_handler_disconnect(sensor, handler);
		g_assert_true(ssc_sensor_accelerometer_close_sync(sensor, NULL, &error));
		g_assert_no_error(error);
		g_object_unref(sensor);
		while (g_main_context_iteration(capture.context, FALSE))
			;
		g_print("cycle=%u samples=%u last=(%.6f,%.6f,%.6f)\n", cycle,
			capture.count, capture.last[0], capture.last[1], capture.last[2]);
	}
	g_main_loop_unref(capture.loop);
	g_main_context_pop_thread_default(capture.context);
	return NULL;
}

int main(void)
{
	GMainContext *context = g_main_context_new();
	guint default_source = g_idle_add(unexpected_default_dispatch, NULL);
	GThread *thread = g_thread_new("ssc-context-test", worker, context);

	g_thread_join(thread);
	g_source_remove(default_source);
	g_main_context_unref(context);
	g_print("PASS: SSC discovery, sampling and close use the worker context\n");
	return 0;
}
