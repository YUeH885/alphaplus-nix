// SPDX-License-Identifier: GPL-2.0
/* Measure sensor control changes against V4L2 frame-start events and RAW10. */
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static uint64_t monotonic_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static double raw10_mean(const uint8_t *data, unsigned int width,
			 unsigned int height, unsigned int stride)
{
	uint64_t sum = 0;
	unsigned int count = 0;

	for (unsigned int y = height / 4; y < height * 3 / 4; y += 16) {
		for (unsigned int x = width / 4; x < width * 3 / 4; x += 4) {
			const uint8_t *p = data + y * stride + x / 4 * 5;

			for (unsigned int i = 0; i < 4; i++) {
				sum += (p[i] << 2) | ((p[4] >> (i * 2)) & 3);
				count++;
			}
		}
	}
	return (double)sum / count;
}

int main(int argc, char **argv)
{
	struct v4l2_event_subscription sub = { .type = V4L2_EVENT_FRAME_SYNC };
	struct v4l2_format fmt = { .type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE };
	struct v4l2_requestbuffers req = {
		.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
		.memory = V4L2_MEMORY_MMAP,
		.count = 8,
	};
	struct v4l2_control ctrl = { 0 };
	void *maps[8] = { 0 };
	size_t lengths[8] = { 0 };
	int video = -1, sensor = -1, events = -1;
	int ret = EXIT_FAILURE, streaming = 0;
	unsigned int frames = 0;
	unsigned int events_seen = 0, changes = 0;
	int low, high;

	if (argc != 7) {
		fprintf(stderr, "Usage: %s VIDEO SENSOR VFE exposure|gain|vblank LOW HIGH\n", argv[0]);
		return EXIT_FAILURE;
	}
	if (!strcmp(argv[4], "exposure"))
		ctrl.id = V4L2_CID_EXPOSURE;
	else if (!strcmp(argv[4], "gain"))
		ctrl.id = V4L2_CID_ANALOGUE_GAIN;
	else if (!strcmp(argv[4], "vblank"))
		ctrl.id = V4L2_CID_VBLANK;
	else {
		fprintf(stderr, "Unknown control: %s\n", argv[4]);
		return EXIT_FAILURE;
	}
	char *end;
	low = strtol(argv[5], &end, 10);
	if (*end || !*argv[5])
		return EXIT_FAILURE;
	high = strtol(argv[6], &end, 10);
	if (*end || !*argv[6])
		return EXIT_FAILURE;

	video = open(argv[1], O_RDWR | O_NONBLOCK);
	sensor = open(argv[2], O_RDWR);
	events = open(argv[3], O_RDWR | O_NONBLOCK);
	if (video < 0 || sensor < 0 || events < 0) {
		perror("open");
		goto out;
	}
	ctrl.value = low;
	if (ioctl(sensor, VIDIOC_S_CTRL, &ctrl) ||
	    ioctl(events, VIDIOC_SUBSCRIBE_EVENT, &sub) ||
	    ioctl(video, VIDIOC_G_FMT, &fmt) ||
	    ioctl(video, VIDIOC_REQBUFS, &req)) {
		perror("configure");
		goto out;
	}
	if (fmt.fmt.pix_mp.pixelformat != V4L2_PIX_FMT_SGBRG10P ||
	    fmt.fmt.pix_mp.num_planes != 1 || req.count > 8 || req.count < 2) {
		fprintf(stderr, "Expected single-plane packed GBRG10 and 2..8 buffers\n");
		goto out;
	}
	for (unsigned int i = 0; i < req.count; i++) {
		struct v4l2_plane plane = { 0 };
		struct v4l2_buffer buf = {
			.type = req.type, .memory = req.memory,
			.index = i, .length = 1, .m.planes = &plane,
		};

		if (ioctl(video, VIDIOC_QUERYBUF, &buf)) {
			perror("query buffer");
			goto out;
		}
		maps[i] = mmap(NULL, plane.length, PROT_READ | PROT_WRITE,
			       MAP_SHARED, video, plane.m.mem_offset);
		if (maps[i] == MAP_FAILED) {
			maps[i] = NULL;
			perror("mmap");
			goto out;
		}
		lengths[i] = plane.length;
		if (ioctl(video, VIDIOC_QBUF, &buf)) {
			perror("queue buffer");
			goto out;
		}
	}
	if (ioctl(video, VIDIOC_STREAMON, &req.type)) {
		perror("stream on");
		goto out;
	}
	streaming = 1;
	puts("kind,sequence,timestamp_ns,value,mean");
	while (frames < 180) {
		struct pollfd fds[] = {
			{ .fd = events, .events = POLLPRI },
			{ .fd = video, .events = POLLIN },
		};

		if (poll(fds, 2, 2000) <= 0) {
			fprintf(stderr, "Capture poll failed or timed out\n");
			goto out;
		}
		if ((fds[0].revents | fds[1].revents) & (POLLERR | POLLHUP | POLLNVAL)) {
			fprintf(stderr, "Capture device poll error\n");
			goto out;
		}
		if (fds[0].revents & POLLPRI) {
			struct v4l2_event event;

			if (ioctl(events, VIDIOC_DQEVENT, &event)) {
				perror("dequeue event");
				goto out;
			}
			unsigned int seq = event.u.frame_sync.frame_sequence;
			events_seen++;
			uint64_t ts = (uint64_t)event.timestamp.tv_sec * 1000000000 +
				      event.timestamp.tv_nsec;
			printf("sof,%u,%" PRIu64 ",%d,\n", seq, ts, ctrl.value);
			if (seq >= 24 && seq % 16 == 8) {
				ctrl.value = ctrl.value == low ? high : low;
				printf("write_begin,%u,%" PRIu64 ",%d,\n", seq, monotonic_ns(), ctrl.value);
				if (ioctl(sensor, VIDIOC_S_CTRL, &ctrl)) {
					perror("set control");
					goto out;
				}
				printf("write_end,%u,%" PRIu64 ",%d,\n", seq, monotonic_ns(), ctrl.value);
				changes++;
			}
		}
		if (fds[1].revents & POLLIN) {
			struct v4l2_plane plane = { 0 };
			struct v4l2_buffer buf = {
				.type = req.type, .memory = req.memory,
				.length = 1, .m.planes = &plane,
			};

			if (ioctl(video, VIDIOC_DQBUF, &buf)) {
				perror("dequeue buffer");
				goto out;
			}
			if (buf.flags & V4L2_BUF_FLAG_ERROR ||
			    plane.bytesused != fmt.fmt.pix_mp.plane_fmt[0].sizeimage) {
				fprintf(stderr, "Incomplete frame\n");
				goto out;
			}
			uint64_t ts = (uint64_t)buf.timestamp.tv_sec * 1000000000 +
				      buf.timestamp.tv_usec * 1000;
			double mean = raw10_mean(maps[buf.index], fmt.fmt.pix_mp.width,
					 fmt.fmt.pix_mp.height,
					 fmt.fmt.pix_mp.plane_fmt[0].bytesperline);
			printf("frame,%u,%" PRIu64 ",%d,%.4f\n", buf.sequence, ts, ctrl.value, mean);
			if (ioctl(video, VIDIOC_QBUF, &buf)) {
				perror("requeue buffer");
				goto out;
			}
			frames++;
		}
	}
	if (events_seen >= frames && changes == 10)
		ret = EXIT_SUCCESS;
	else
		fprintf(stderr, "Expected frame-start events and ten control changes\n");
out:
	if (streaming && ioctl(video, VIDIOC_STREAMOFF, &req.type)) {
		perror("stream off");
		ret = EXIT_FAILURE;
	}
	for (unsigned int i = 0; i < 8; i++)
		if (maps[i])
			munmap(maps[i], lengths[i]);
	if (events >= 0)
		close(events);
	if (sensor >= 0)
		close(sensor);
	if (video >= 0)
		close(video);
	return ret;
}
