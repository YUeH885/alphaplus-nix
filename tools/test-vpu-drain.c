/* SPDX-License-Identifier: MIT */
/* Drain an incomplete sequence before the first CAPTURE format event. */
#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <poll.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static void checked_ioctl(int fd, unsigned long request, void *argument)
{
	if (ioctl(fd, request, argument) < 0) {
		fprintf(stderr, "ioctl %#lx failed: %s\n", request, strerror(errno));
		exit(EXIT_FAILURE);
	}
}

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
	enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
	struct v4l2_capability capability = {0};
	struct v4l2_format format = {.type = type};
	struct v4l2_requestbuffers request = {
		.type = type, .memory = V4L2_MEMORY_MMAP, .count = 1,
	};
	struct v4l2_plane plane = {0};
	struct v4l2_buffer buffer = {
		.type = type, .memory = V4L2_MEMORY_MMAP,
		.length = 1, .m.planes = &plane,
	};
	struct v4l2_decoder_cmd command = {.cmd = V4L2_DEC_CMD_STOP};
	struct v4l2_event_subscription subscription = {.type = V4L2_EVENT_SOURCE_CHANGE};
	struct pollfd descriptor;
	bool source_change = false, expect_timeout;
	long long start, deadline;
	size_t size, mapped_size;
	void *mapped;
	FILE *input;
	int fd, result = EXIT_FAILURE;

	if (argc != 5 || (strcmp(argv[2], "h264") && strcmp(argv[2], "hevc")) ||
	    (strcmp(argv[4], "timeout") && strcmp(argv[4], "source-change"))) {
		fprintf(stderr, "Usage: %s DEVICE h264|hevc INPUT timeout|source-change\n", argv[0]);
		return EXIT_FAILURE;
	}
	expect_timeout = !strcmp(argv[4], "timeout");
	fd = open(argv[1], O_RDWR | O_NONBLOCK);
	if (fd < 0) {
		perror("open device");
		return EXIT_FAILURE;
	}
	checked_ioctl(fd, VIDIOC_QUERYCAP, &capability);
	if (strcmp((char *)capability.driver, "iris_driver") ||
	    strcmp((char *)capability.card, "Iris Decoder")) {
		fprintf(stderr, "Expected Iris Decoder\n");
		close(fd);
		return EXIT_FAILURE;
	}
	format.fmt.pix_mp = (struct v4l2_pix_format_mplane){
		.width = 320, .height = 240, .field = V4L2_FIELD_NONE,
		.pixelformat = !strcmp(argv[2], "h264") ? V4L2_PIX_FMT_H264 : V4L2_PIX_FMT_HEVC,
	};
	checked_ioctl(fd, VIDIOC_S_FMT, &format);
	checked_ioctl(fd, VIDIOC_SUBSCRIBE_EVENT, &subscription);
	subscription.type = V4L2_EVENT_EOS;
	checked_ioctl(fd, VIDIOC_SUBSCRIBE_EVENT, &subscription);
	checked_ioctl(fd, VIDIOC_REQBUFS, &request);
	checked_ioctl(fd, VIDIOC_QUERYBUF, &buffer);
	mapped_size = plane.length;
	mapped = mmap(NULL, mapped_size, PROT_READ | PROT_WRITE, MAP_SHARED,
		      fd, plane.m.mem_offset);
	if (mapped == MAP_FAILED) {
		perror("mmap");
		close(fd);
		return EXIT_FAILURE;
	}
	input = fopen(argv[3], "rb");
	if (!input) {
		perror("fopen input");
		goto cleanup;
	}
	size = fread(mapped, 1, mapped_size, input);
	if (!size || ferror(input) || fgetc(input) != EOF) {
		fprintf(stderr, "Expected a nonempty input fitting one OUTPUT buffer\n");
		fclose(input);
		goto cleanup;
	}
	fclose(input);
	plane.bytesused = size;
	checked_ioctl(fd, VIDIOC_QBUF, &buffer);
	checked_ioctl(fd, VIDIOC_STREAMON, &type);
	start = milliseconds();
	if (ioctl(fd, VIDIOC_DECODER_CMD, &command) < 0) {
		int error = errno;

		printf("STOP returned %s in %lld ms\n", strerror(error), milliseconds() - start);
		if (!expect_timeout || error != ETIMEDOUT || milliseconds() - start >= 3000)
			goto streamoff;
		descriptor = (struct pollfd){.fd = fd, .events = POLLIN | POLLOUT | POLLPRI};
		if (poll(&descriptor, 1, 0) != 1 || !(descriptor.revents & POLLERR)) {
			fprintf(stderr, "Expected POLLERR after a failed drain\n");
			goto streamoff;
		}
		result = EXIT_SUCCESS;
		goto streamoff;
	}
	if (expect_timeout) {
		fprintf(stderr, "STOP accepted an incomplete sequence\n");
		goto streamoff;
	}
	deadline = start + 3000;
	while (!source_change) {
		int remaining = deadline - milliseconds();
		struct v4l2_event event = {0};

		if (remaining <= 0) {
			fprintf(stderr, "Initial SOURCE_CHANGE timed out\n");
			goto streamoff;
		}
		descriptor = (struct pollfd){
			.fd = fd, .events = POLLPRI,
		};
		if (poll(&descriptor, 1, remaining) < 0) {
			perror("poll");
			goto streamoff;
		}
		while (ioctl(fd, VIDIOC_DQEVENT, &event) == 0) {
			printf("Event: type=%u\n", event.type);
			if (event.type == V4L2_EVENT_SOURCE_CHANGE) {
				source_change = true;
			}
			if (event.type == V4L2_EVENT_EOS) {
				fprintf(stderr, "EOS arrived before starting CAPTURE\n");
				goto streamoff;
			}
		}
		if (errno != EAGAIN && errno != ENOENT) {
			perror("VIDIOC_DQEVENT");
			goto streamoff;
		}
	}
	printf("Initial SOURCE_CHANGE received in %lld ms\n", milliseconds() - start);
	result = EXIT_SUCCESS;
streamoff:
	checked_ioctl(fd, VIDIOC_STREAMOFF, &type);
cleanup:
	munmap(mapped, mapped_size);
	close(fd);
	return result;
}
