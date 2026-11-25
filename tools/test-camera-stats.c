// SPDX-License-Identifier: GPL-2.0
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#include <linux/qcom_camss.h>
#include <linux/videodev2.h>

#define AWB_SIZE (V4L2_QCOM_CAMSS_AWB_BG_REGIONS * \
		  V4L2_QCOM_CAMSS_AWB_BG_REGION_SIZE)
#define HIST_SIZE (V4L2_QCOM_CAMSS_BHIST_BINS * \
		   V4L2_QCOM_CAMSS_BHIST_CHANNELS * sizeof(uint32_t))
#define BF_SIZE V4L2_QCOM_CAMSS_BF_SIZE
#define DATA_SIZE (AWB_SIZE + HIST_SIZE + BF_SIZE)

_Static_assert(sizeof(struct v4l2_qcom_camss_stats) == 72, "Invalid stats ABI");

static uint64_t monotonic_ms(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int check_invalid_requests(int fd,
				 const struct v4l2_qcom_camss_stats *valid)
{
	struct v4l2_qcom_camss_stats invalid;
	unsigned int i;

	for (i = 0; i < 4; i++) {
		int expected = i == 3 ? EFAULT : EINVAL;

		invalid = *valid;
		if (i == 0)
			invalid.version--;
		else if (i == 1)
			invalid.version++;
		else if (i == 2)
			invalid.reserved = 1;
		else
			invalid.awb_bg = 1;
		errno = 0;
		if (ioctl(fd, VIDIOC_QCOM_CAMSS_GET_STATS, &invalid) != -1 ||
		    errno != expected) {
			fprintf(stderr, "Invalid request %u: expected errno %d, got %d\n",
				i, expected, errno);
			return -1;
		}
	}
	return 0;
}

int main(int argc, char **argv)
{
	struct v4l2_qcom_camss_stats stats = {
		.version = V4L2_QCOM_CAMSS_STATS_VERSION,
	};
	uint64_t previous_timestamp = 0;
	uint32_t previous_sequence = 0;
	unsigned int frames = 10, got = 0;
	uint64_t deadline;
	uint8_t *data;
	uint32_t *awb, *bhist;
	uint64_t *bf;
	int fd, ret = EXIT_FAILURE;

	if (argc < 2 || argc > 4) {
		fprintf(stderr, "Usage: %s VIDEO [FRAMES] [RAW_OUTPUT]\n", argv[0]);
		return EXIT_FAILURE;
	}
	if (argc >= 3) {
		char *end;
		unsigned long value;

		errno = 0;
		value = strtoul(argv[2], &end, 0);
		if (errno || *end || !value || value > UINT_MAX) {
			fprintf(stderr, "Invalid frame count: %s\n", argv[2]);
			return EXIT_FAILURE;
		}
		frames = value;
	}
	fd = open(argv[1], O_RDWR);
	if (fd < 0) {
		perror("open video");
		return EXIT_FAILURE;
	}
	data = calloc(1, DATA_SIZE);
	if (!data) {
		perror("calloc");
		close(fd);
		return EXIT_FAILURE;
	}
	awb = (uint32_t *)data;
	bhist = (uint32_t *)(data + AWB_SIZE);
	bf = (uint64_t *)(data + AWB_SIZE + HIST_SIZE);
	stats.awb_bg = (uintptr_t)awb;
	stats.bhist = (uintptr_t)bhist;
	stats.bf = (uintptr_t)bf;
	deadline = monotonic_ms() + 5000;

	while (got < frames) {
		uint64_t hist_sum = 0, h_focus = 0, v_focus = 0;
		unsigned int nonzero = 0, i;
		int result = ioctl(fd, VIDIOC_QCOM_CAMSS_GET_STATS, &stats);

		if (result < 0 && errno != EAGAIN && errno != ENODATA) {
			perror("VIDIOC_QCOM_CAMSS_GET_STATS");
			goto out;
		}
		if (result < 0 || (got && stats.sequence == previous_sequence)) {
			const struct timespec delay = { .tv_nsec = 2000000 };

			if (monotonic_ms() >= deadline) {
				fprintf(stderr, "Timed out waiting for fresh statistics\n");
				goto out;
			}
			nanosleep(&delay, NULL);
			continue;
		}
		if (stats.awb_bg_regions != V4L2_QCOM_CAMSS_AWB_BG_REGIONS ||
		    stats.bhist_bins != V4L2_QCOM_CAMSS_BHIST_BINS ||
		    stats.bf_regions != V4L2_QCOM_CAMSS_BF_ROIS ||
		    (got && (stats.sequence < previous_sequence ||
			     stats.timestamp <= previous_timestamp))) {
			fprintf(stderr, "Invalid statistics dimensions or frame order\n");
			goto out;
		}
		if (!got && check_invalid_requests(fd, &stats))
			goto out;
		for (i = 0; i < stats.awb_bg_regions; i++) {
			uint32_t *region = awb + i * 12;
			uint32_t count = region[4] & 0xffff;

			if (!count || (region[4] >> 16) != count ||
			    (region[5] & 0xffff) != count ||
			    (region[5] >> 16) != count) {
				fprintf(stderr, "Invalid AWB region %u counts\n", i);
				goto out;
			}
		}
		for (i = 0; i < HIST_SIZE / sizeof(*bhist); i++) {
			if (bhist[i] & ~0x1ffffffU) {
				fprintf(stderr, "Invalid BHIST bin %u\n", i);
				goto out;
			}
			nonzero += bhist[i] != 0;
			hist_sum += bhist[i];
		}
		for (i = 0; i < stats.bf_regions; i++) {
			uint64_t *roi = bf + 1 + 4 * i;

			if (((roi[1] >> 54) & 0xff) != i) {
				fprintf(stderr, "Invalid BF region %u\n", i);
				goto out;
			}
			h_focus += roi[1] & 0xffffffffffULL;
			v_focus += roi[3] & 0xffffffffffULL;
		}
		if (!hist_sum) {
			fprintf(stderr, "Empty BHIST statistics\n");
			goto out;
		}
		printf("seq %u ts %llu awb_regions %u bhist_bins %u bf_regions %u "
		       "awb_count %u hist_nonzero %u hist_sum %" PRIu64 " bf_h %" PRIu64
		       " bf_v %" PRIu64 " sensor_valid %u exposure %u gain %u\n", stats.sequence,
		       (unsigned long long)stats.timestamp, stats.awb_bg_regions,
		       stats.bhist_bins, stats.bf_regions, awb[4] & 0xffff, nonzero, hist_sum,
		       h_focus, v_focus, !!(stats.flags & V4L2_QCOM_CAMSS_STATS_SENSOR_CONTROLS),
		       stats.exposure_lines, stats.analogue_gain);
		previous_sequence = stats.sequence;
		previous_timestamp = stats.timestamp;
		deadline = monotonic_ms() + 2000;
		got++;
	}
	if (argc == 4) {
		int output = open(argv[3], O_WRONLY | O_CREAT | O_TRUNC, 0644);

		if (output < 0) {
			perror("open output");
			goto out;
		}
		if (write(output, data, DATA_SIZE) != DATA_SIZE) {
			perror("write output");
			close(output);
			goto out;
		}
		if (close(output)) {
			perror("close output");
			goto out;
		}
	}
	printf("PASS: %u distinct statistics frames, %zu bytes/snapshot\n",
	       got, (size_t)DATA_SIZE);
	ret = EXIT_SUCCESS;
out:
	free(data);
	close(fd);
	return ret;
}
