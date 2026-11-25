/* SPDX-License-Identifier: MIT */
/* Check drain directly, without an avformat probe. */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <libavcodec/avcodec.h>
#include <libavutil/error.h>

static long long milliseconds(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) {
		perror("clock_gettime");
		exit(EXIT_FAILURE);
	}
	return (long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

int main(int argc, char **argv)
{
	const AVCodec *decoder;
	AVCodecContext *context = NULL;
	AVPacket *packet = NULL;
	AVFrame *frame = NULL;
	FILE *input;
	unsigned char data[65536];
	size_t size;
	unsigned int frames = 0;
	int expected;
	long long start, elapsed;
	int ret = AVERROR(EINVAL), result = EXIT_FAILURE;

	if (argc != 4 || (strcmp(argv[1], "h264") && strcmp(argv[1], "hevc")) ||
	    (strcmp(argv[3], "timeout") && strcmp(argv[3], "frame"))) {
		fprintf(stderr, "Usage: %s h264|hevc INPUT timeout|frame\n", argv[0]);
		return EXIT_FAILURE;
	}
	expected = !strcmp(argv[3], "timeout") ? AVERROR(ETIMEDOUT) : AVERROR_EOF;
	input = fopen(argv[2], "rb");
	if (!input) {
		perror("fopen input");
		return EXIT_FAILURE;
	}
	size = fread(data, 1, sizeof(data), input);
	if (!size || ferror(input) || fgetc(input) != EOF) {
		fprintf(stderr, "Expected 1..65536 input bytes\n");
		fclose(input);
		return EXIT_FAILURE;
	}
	fclose(input);
	decoder = avcodec_find_decoder_by_name(!strcmp(argv[1], "h264") ?
					       "h264_v4l2m2m" : "hevc_v4l2m2m");
	if (!decoder) {
		fprintf(stderr, "V4L2 decoder unavailable\n");
		return EXIT_FAILURE;
	}
	context = avcodec_alloc_context3(decoder);
	packet = av_packet_alloc();
	frame = av_frame_alloc();
	if (!context || !packet || !frame) {
		ret = AVERROR(ENOMEM);
		goto cleanup;
	}
	context->width = context->coded_width = 320;
	context->height = context->coded_height = 240;
	fprintf(stderr, "Opening decoder\n");
	ret = avcodec_open2(context, decoder, NULL);
	if (ret < 0)
		goto cleanup;
	ret = av_new_packet(packet, size);
	if (ret < 0)
		goto cleanup;
	memcpy(packet->data, data, packet->size);
	fprintf(stderr, "Submitting input\n");
	ret = avcodec_send_packet(context, packet);
	if (ret < 0)
		goto cleanup;
	start = milliseconds();
	fprintf(stderr, "Starting drain\n");
	ret = avcodec_send_packet(context, NULL);
	if (ret < 0)
		goto cleanup;
	fprintf(stderr, "Receiving drain result\n");
	while ((ret = avcodec_receive_frame(context, frame)) >= 0) {
		if (frame->width != 320 || frame->height != 240) {
			fprintf(stderr, "Unexpected frame size: %dx%d\n", frame->width, frame->height);
			goto cleanup;
		}
		frames++;
		av_frame_unref(frame);
	}
	elapsed = milliseconds() - start;
	fprintf(stderr, "Drain returned %s in %lld ms; frames=%u\n", av_err2str(ret), elapsed, frames);
	if (ret == expected && frames == (expected == AVERROR_EOF ? 1U : 0U) && elapsed < 3000)
		result = EXIT_SUCCESS;
cleanup:
	if (result != EXIT_SUCCESS)
		fprintf(stderr, "Unexpected result: %s\n", av_err2str(ret));
	av_frame_free(&frame);
	av_packet_free(&packet);
	avcodec_free_context(&context);
	return result;
}
