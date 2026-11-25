#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-or-later
# Manage display power state during SDDM greeter inactivity.

exec /usr/bin/swayidle -w \
	timeout 30 '/usr/bin/kscreen-doctor --dpms off' \
	resume '/usr/bin/kscreen-doctor --dpms on'
