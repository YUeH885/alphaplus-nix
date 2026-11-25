/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Validate exported decoder frames by downloading their visible pixels. */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_drm.h>
#include <libavutil/imgutils.h>
#include <libavutil/log.h>
#include <libavutil/pixdesc.h>

static enum AVPixelFormat select_drm(AVCodecContext *context,
                                    const enum AVPixelFormat *formats)
{
    (void)context;
    for (; *formats != AV_PIX_FMT_NONE; formats++)
        if (*formats == AV_PIX_FMT_DRM_PRIME)
            return *formats;
    return AV_PIX_FMT_NONE;
}

struct decode_state {
    int count;
    int width, height;
    enum AVPixelFormat format;
    AVFrame *held;
};

static int receive_frames(AVCodecContext *context, FILE *output, struct decode_state *state)
{
    AVFrame *frame = av_frame_alloc();
    AVFrame *download = av_frame_alloc();
    uint8_t *pixels = NULL;
    int ret = AVERROR(ENOMEM);

    if (!frame || !download)
        goto out;
    while ((ret = avcodec_receive_frame(context, frame)) >= 0) {
        const AVDRMFrameDescriptor *descriptor;
        int size;

        if (frame->format != AV_PIX_FMT_DRM_PRIME || !frame->hw_frames_ctx) {
            ret = AVERROR(EINVAL);
            goto out;
        }
        descriptor = (const AVDRMFrameDescriptor *)frame->data[0];
        const AVHWFramesContext *frames = (const AVHWFramesContext *)frame->hw_frames_ctx->data;
        if (!state->count || state->width != frame->width || state->height != frame->height ||
            state->format != frames->sw_format) {
            fprintf(stderr, "DRM frame: %dx%d %s at frame=%d, objects=%d, layers=%d\n",
                    frame->width, frame->height, av_get_pix_fmt_name(frames->sw_format),
                    state->count, descriptor->nb_objects, descriptor->nb_layers);
            for (int i = 0; i < descriptor->nb_layers; i++)
                for (int j = 0; j < descriptor->layers[i].nb_planes; j++) {
                    const AVDRMPlaneDescriptor *plane = &descriptor->layers[i].planes[j];
                    fprintf(stderr, "plane=%d object=%d offset=%td pitch=%td\n",
                            j, plane->object_index, plane->offset, plane->pitch);
                }
            state->width = frame->width;
            state->height = frame->height;
            state->format = frames->sw_format;
        }
        if (!state->count && state->held) {
            ret = av_frame_ref(state->held, frame);
            if (ret < 0)
                goto out;
        }
        ret = av_hwframe_transfer_data(download, frame, 0);
        if (ret < 0)
            goto out;
        size = av_image_get_buffer_size(download->format, frame->width, frame->height, 1);
        if (size < 0) {
            ret = size;
            goto out;
        }
        pixels = av_malloc(size);
        if (!pixels) {
            ret = AVERROR(ENOMEM);
            goto out;
        }
        ret = av_image_copy_to_buffer(pixels, size, (const uint8_t *const *)download->data,
                                      download->linesize, download->format,
                                      frame->width, frame->height, 1);
        if (ret < 0)
            goto out;
        if (fwrite(pixels, 1, size, output) != (size_t)size) {
            ret = AVERROR(EIO);
            goto out;
        }
        av_freep(&pixels);
        av_frame_unref(download);
        av_frame_unref(frame);
        state->count++;
    }
out:
    av_free(pixels);
    av_frame_free(&download);
    av_frame_free(&frame);
    return ret;
}

static int verify_held_frame(const AVFrame *held, FILE *output)
{
    AVFrame *download = av_frame_alloc();
    uint8_t *pixels = NULL, *reference = NULL;
    int ret = AVERROR(ENOMEM), size;

    if (!download)
        goto out;
    ret = av_hwframe_transfer_data(download, held, 0);
    if (ret < 0)
        goto out;
    size = av_image_get_buffer_size(download->format, held->width, held->height, 1);
    if (size < 0) {
        ret = size;
        goto out;
    }
    pixels = av_malloc(size);
    reference = av_malloc(size);
    if (!pixels || !reference) {
        ret = AVERROR(ENOMEM);
        goto out;
    }
    ret = av_image_copy_to_buffer(pixels, size, (const uint8_t *const *)download->data,
                                  download->linesize, download->format,
                                  held->width, held->height, 1);
    if (ret < 0)
        goto out;
    if (fseek(output, 0, SEEK_SET) || fread(reference, 1, size, output) != (size_t)size) {
        ret = AVERROR(EIO);
        goto out;
    }
    ret = memcmp(pixels, reference, size) ? AVERROR_INVALIDDATA : 0;
    if (!ret)
        fprintf(stderr, "Held frame unchanged after decoder close\n");
out:
    av_free(pixels);
    av_free(reference);
    av_frame_free(&download);
    return ret;
}

int main(int argc, char **argv)
{
    AVFormatContext *input = NULL;
    AVCodecContext *context = NULL;
    AVPacket *packet = NULL;
    const AVCodec *decoder;
    FILE *output = NULL;
    const char *name;
    struct decode_state state = {0};
    int index, ret;

    if ((argc != 3 && argc != 4) || (argc == 4 && strcmp(argv[3], "--hold-first-frame"))) {
        fprintf(stderr, "Usage: %s INPUT OUTPUT [--hold-first-frame]\n", argv[0]);
        return 2;
    }
    if (argc == 4) {
        state.held = av_frame_alloc();
        if (!state.held)
            return 1;
    }
    av_log_set_level(AV_LOG_DEBUG);
    ret = avformat_open_input(&input, argv[1], NULL, NULL);
    if (ret < 0)
        goto out;
    ret = avformat_find_stream_info(input, NULL);
    if (ret < 0)
        goto out;
    index = av_find_best_stream(input, AVMEDIA_TYPE_VIDEO, -1, -1, NULL, 0);
    if (index < 0) {
        ret = index;
        goto out;
    }
    if (input->streams[index]->codecpar->width <= 0 ||
        input->streams[index]->codecpar->height <= 0) {
        fprintf(stderr, "No valid video sequence header found\n");
        ret = AVERROR_INVALIDDATA;
        goto out;
    }
    switch (input->streams[index]->codecpar->codec_id) {
    case AV_CODEC_ID_H264: name = "h264_v4l2m2m"; break;
    case AV_CODEC_ID_HEVC: name = "hevc_v4l2m2m"; break;
    case AV_CODEC_ID_VP9: name = "vp9_v4l2m2m"; break;
    default: ret = AVERROR(EINVAL); goto out;
    }
    decoder = avcodec_find_decoder_by_name(name);
    if (!decoder) {
        ret = AVERROR_DECODER_NOT_FOUND;
        goto out;
    }
    context = avcodec_alloc_context3(decoder);
    packet = av_packet_alloc();
    if (!context || !packet) {
        ret = AVERROR(ENOMEM);
        goto out;
    }
    ret = avcodec_parameters_to_context(context, input->streams[index]->codecpar);
    if (ret < 0)
        goto out;
    context->get_format = select_drm;
    context->pkt_timebase = input->streams[index]->time_base;
    ret = avcodec_open2(context, decoder, NULL);
    if (ret < 0)
        goto out;
    output = fopen(argv[2], "w+bx");
    if (!output) {
        ret = AVERROR(errno);
        goto out;
    }
    while ((ret = av_read_frame(input, packet)) >= 0) {
        if (packet->stream_index == index) {
            ret = avcodec_send_packet(context, packet);
            if (ret < 0)
                goto out;
            ret = receive_frames(context, output, &state);
            if (ret != AVERROR(EAGAIN))
                goto out;
        }
        av_packet_unref(packet);
    }
    if (ret != AVERROR_EOF)
        goto out;
    ret = avcodec_send_packet(context, NULL);
    if (ret < 0)
        goto out;
    ret = receive_frames(context, output, &state);
    if (ret == AVERROR_EOF && state.count > 0)
        ret = 0;
out:
    avcodec_free_context(&context);
    if (ret >= 0 && state.held)
        ret = verify_held_frame(state.held, output);
    av_frame_free(&state.held);
    if (output && fclose(output) && ret >= 0)
        ret = AVERROR(errno);
    if (ret < 0)
        fprintf(stderr, "Decode failed: %s\n", av_err2str(ret));
    fprintf(stderr, "Decoded frames: %d\n", state.count);
    av_packet_free(&packet);
    avformat_close_input(&input);
    return ret < 0;
}
