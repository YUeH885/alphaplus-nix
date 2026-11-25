/* SPDX-License-Identifier: MIT */
/* Exercise both STREAMON orders and STOP/LAST/START within one encoder session. */
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
#include <unistd.h>

#define BUFFERS 4

struct mapped_buffer {
	void *data;
	size_t size;
	bool queued;
};

static void fail(const char *operation)
{
	perror(operation);
	exit(EXIT_FAILURE);
}

static void checked_ioctl(int fd, unsigned long request, void *argument)
{
	int ret;

	do {
		ret = ioctl(fd, request, argument);
	} while (ret < 0 && errno == EINTR);
	if (ret < 0) {
		fprintf(stderr, "ioctl %#lx failed: %s\n", request, strerror(errno));
		exit(EXIT_FAILURE);
	}
}

static void map_buffers(int fd, enum v4l2_buf_type type, struct mapped_buffer *buffers)
{
	struct v4l2_requestbuffers request = {
		.count = BUFFERS, .type = type, .memory = V4L2_MEMORY_MMAP,
	};

	checked_ioctl(fd, VIDIOC_REQBUFS, &request);
	if (request.count != BUFFERS) {
		fprintf(stderr, "Expected %u buffers, got %u\n", BUFFERS, request.count);
		exit(EXIT_FAILURE);
	}
	for (unsigned int i = 0; i < BUFFERS; i++) {
		struct v4l2_plane plane = {0};
		struct v4l2_buffer buffer = {
			.index = i, .type = type, .memory = V4L2_MEMORY_MMAP,
			.length = 1, .m.planes = &plane,
		};

		checked_ioctl(fd, VIDIOC_QUERYBUF, &buffer);
		buffers[i].size = plane.length;
		buffers[i].data = mmap(NULL, plane.length, PROT_READ | PROT_WRITE,
				       MAP_SHARED, fd, plane.m.mem_offset);
		if (buffers[i].data == MAP_FAILED)
			fail("mmap");
	}
}

static void queue(int fd, enum v4l2_buf_type type, unsigned int index,
		  struct mapped_buffer *mapped, unsigned int frame)
{
	struct v4l2_plane plane = {.length = mapped->size};
	struct v4l2_buffer buffer = {
		.index = index, .type = type, .memory = V4L2_MEMORY_MMAP,
		.length = 1, .m.planes = &plane,
	};

	if (type == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE) {
		plane.bytesused = mapped->size;
		buffer.timestamp.tv_sec = frame / 30;
		buffer.timestamp.tv_usec = (frame % 30) * 1000000 / 30;
	}
	checked_ioctl(fd, VIDIOC_QBUF, &buffer);
	mapped->queued = true;
}

static bool dequeue(int fd, enum v4l2_buf_type type, struct v4l2_buffer *buffer,
		    struct v4l2_plane *plane)
{
	*plane = (struct v4l2_plane){0};
	*buffer = (struct v4l2_buffer){
		.type = type, .memory = V4L2_MEMORY_MMAP, .length = 1, .m.planes = plane,
	};
	if (ioctl(fd, VIDIOC_DQBUF, buffer) < 0) {
		if (errno == EAGAIN)
			return false;
		fail("VIDIOC_DQBUF");
	}
	if (buffer->index >= BUFFERS || buffer->flags & V4L2_BUF_FLAG_ERROR) {
		fprintf(stderr, "Invalid dequeued buffer: index=%u flags=%#x\n",
			buffer->index, buffer->flags);
		exit(EXIT_FAILURE);
	}
	return true;
}

static void fill(struct mapped_buffer *buffer, const unsigned char *raw,
		 unsigned int width, unsigned int height, unsigned int stride,
		 unsigned int scanlines)
{
	memset(buffer->data, 0, buffer->size);
	for (unsigned int row = 0; row < height; row++)
		memcpy((unsigned char *)buffer->data + row * stride, raw + row * width, width);
	for (unsigned int row = 0; row < height / 2; row++)
		memcpy((unsigned char *)buffer->data + stride * (scanlines + row),
		       raw + width * (height + row), width);
}

int main(int argc, char **argv)
{
	struct mapped_buffer input[BUFFERS] = {0}, capture[BUFFERS] = {0};
	struct v4l2_capability capability = {0};
	struct v4l2_format format = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE};
	struct v4l2_streamparm rate = {.type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE};
	struct v4l2_control control = {.id = V4L2_CID_MPEG_VIDEO_GOP_SIZE, .value = 30};
	enum v4l2_buf_type input_type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
	enum v4l2_buf_type capture_type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
	unsigned int width, height, frames, stride, scanlines;
	unsigned char *raw;
	size_t frame_size;
	FILE *source;
	int fd;

	if (argc != 8 && argc != 9) {
		fprintf(stderr, "Usage: %s DEVICE h264|hevc INPUT.nv12 WIDTH HEIGHT FRAMES PREFIX [capture-first]\n", argv[0]);
		return EXIT_FAILURE;
	}
	width = strtoul(argv[4], NULL, 10);
	height = strtoul(argv[5], NULL, 10);
	frames = strtoul(argv[6], NULL, 10);
	if (!width || !height || width > 8192 || height > 8192 || width % 2 || height % 2 ||
	    !frames || frames > 10000 || (strcmp(argv[2], "h264") && strcmp(argv[2], "hevc")) ||
	    (argc == 9 && strcmp(argv[8], "capture-first"))) {
		fprintf(stderr, "Invalid test parameters\n");
		return EXIT_FAILURE;
	}
	frame_size = (size_t)width * height * 3 / 2;
	raw = malloc(frame_size * frames);
	if (!raw)
		fail("malloc");
	source = fopen(argv[3], "rb");
	if (!source)
		fail("fopen input");
	if (fread(raw, frame_size, frames, source) != frames || fgetc(source) != EOF) {
		fprintf(stderr, "Expected exactly %u NV12 frames\n", frames);
		return EXIT_FAILURE;
	}
	fclose(source);
	fd = open(argv[1], O_RDWR | O_NONBLOCK);
	if (fd < 0)
		fail("open device");
	checked_ioctl(fd, VIDIOC_QUERYCAP, &capability);
	if (strcmp((char *)capability.driver, "iris_driver") ||
	    strcmp((char *)capability.card, "Iris Encoder")) {
		fprintf(stderr, "Expected Iris Encoder\n");
		return EXIT_FAILURE;
	}
	format.fmt.pix_mp = (struct v4l2_pix_format_mplane){
		.width = width, .height = height, .field = V4L2_FIELD_NONE,
		.pixelformat = !strcmp(argv[2], "h264") ? V4L2_PIX_FMT_H264 : V4L2_PIX_FMT_HEVC,
	};
	checked_ioctl(fd, VIDIOC_S_FMT, &format);
	format.type = input_type;
	format.fmt.pix_mp = (struct v4l2_pix_format_mplane){
		.width = width, .height = height, .pixelformat = V4L2_PIX_FMT_NV12,
		.field = V4L2_FIELD_NONE,
	};
	checked_ioctl(fd, VIDIOC_S_FMT, &format);
	stride = format.fmt.pix_mp.plane_fmt[0].bytesperline;
	scanlines = format.fmt.pix_mp.height;
	if (stride < width || scanlines < height || format.fmt.pix_mp.num_planes != 1) {
		fprintf(stderr, "Unexpected NV12 layout\n");
		return EXIT_FAILURE;
	}
	rate.type = input_type;
	rate.parm.output.timeperframe = (struct v4l2_fract){1, 60};
	checked_ioctl(fd, VIDIOC_S_PARM, &rate);
	rate.type = capture_type;
	checked_ioctl(fd, VIDIOC_G_PARM, &rate);
	if (rate.parm.capture.timeperframe.numerator != 1 ||
	    rate.parm.capture.timeperframe.denominator != 60) {
		fprintf(stderr, "OUTPUT frame interval did not update CAPTURE\n");
		return EXIT_FAILURE;
	}
	rate.type = input_type;
	rate.parm.output.timeperframe = (struct v4l2_fract){1, 30};
	checked_ioctl(fd, VIDIOC_S_PARM, &rate);
	checked_ioctl(fd, VIDIOC_S_CTRL, &control);
	map_buffers(fd, input_type, input);
	map_buffers(fd, capture_type, capture);
	for (unsigned int i = 0; i < BUFFERS; i++) {
		if (input[i].size < (size_t)stride * scanlines * 3 / 2) {
			fprintf(stderr, "Input buffer is too small for the negotiated layout\n");
			return EXIT_FAILURE;
		}
	}
	for (unsigned int cycle = 0; cycle < 2; cycle++) {
		unsigned int submitted = 0, completed = 0;
		bool draining = false, last = false;
		char path[4096];
		FILE *output;

		if (snprintf(path, sizeof(path), "%s-%u.%s", argv[7], cycle, argv[2]) >= (int)sizeof(path)) {
			fprintf(stderr, "Output path is too long\n");
			return EXIT_FAILURE;
		}
		output = fopen(path, "wb");
		if (!output)
			fail("fopen output");
		if (cycle) {
			struct v4l2_encoder_cmd command = {.cmd = V4L2_ENC_CMD_START};

			checked_ioctl(fd, VIDIOC_ENCODER_CMD, &command);
		}
		for (unsigned int i = 0; i < BUFFERS; i++) {
			if (!capture[i].queued)
				queue(fd, capture_type, i, &capture[i], 0);
			if (submitted < frames) {
				fill(&input[i], raw + frame_size * submitted, width, height, stride, scanlines);
				queue(fd, input_type, i, &input[i], cycle * frames + submitted++);
			}
		}
		if (!cycle) {
			checked_ioctl(fd, VIDIOC_STREAMON, argc == 9 ? &capture_type : &input_type);
			checked_ioctl(fd, VIDIOC_STREAMON, argc == 9 ? &input_type : &capture_type);
		}
		while (!last) {
			struct pollfd wait = {.fd = fd, .events = POLLIN | POLLOUT};
			struct v4l2_plane plane;
			struct v4l2_buffer buffer;
			int ret = poll(&wait, 1, 10000);

			if (ret <= 0 || wait.revents & (POLLERR | POLLHUP | POLLNVAL)) {
				fprintf(stderr, "Encoder poll failed: result=%d events=%#x\n", ret, wait.revents);
				return EXIT_FAILURE;
			}
			while (dequeue(fd, input_type, &buffer, &plane)) {
				input[buffer.index].queued = false;
				completed++;
				if (submitted < frames) {
					fill(&input[buffer.index], raw + frame_size * submitted, width, height, stride, scanlines);
					queue(fd, input_type, buffer.index, &input[buffer.index], cycle * frames + submitted++);
				}
			}
			while (dequeue(fd, capture_type, &buffer, &plane)) {
				struct mapped_buffer *mapped = &capture[buffer.index];

				mapped->queued = false;
				if (plane.data_offset > plane.bytesused || plane.bytesused > mapped->size) {
					fprintf(stderr, "Invalid capture payload\n");
					return EXIT_FAILURE;
				}
				if (fwrite((unsigned char *)mapped->data + plane.data_offset, 1,
					   plane.bytesused - plane.data_offset, output) != plane.bytesused - plane.data_offset)
					fail("fwrite");
				if (buffer.flags & V4L2_BUF_FLAG_LAST) {
					last = true;
					break;
				}
				queue(fd, capture_type, buffer.index, mapped, 0);
			}
			if (completed == frames && !draining) {
				struct v4l2_encoder_cmd command = {.cmd = V4L2_ENC_CMD_STOP};

				checked_ioctl(fd, VIDIOC_ENCODER_CMD, &command);
				draining = true;
			}
		}
		if (completed != frames || fclose(output)) {
			fprintf(stderr, "Incomplete drain: %u/%u input buffers\n", completed, frames);
			return EXIT_FAILURE;
		}
		printf("cycle %u: %u input buffers completed, LAST received\n", cycle, completed);
	}
	checked_ioctl(fd, VIDIOC_STREAMOFF, &input_type);
	checked_ioctl(fd, VIDIOC_STREAMOFF, &capture_type);
	for (unsigned int i = 0; i < BUFFERS; i++) {
		munmap(input[i].data, input[i].size);
		munmap(capture[i].data, capture[i].size);
	}
	close(fd);
	free(raw);
	return EXIT_SUCCESS;
}
