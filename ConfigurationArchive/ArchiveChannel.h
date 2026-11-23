#ifndef SILEO_ARCHIVE_CHANNEL_H
#define SILEO_ARCHIVE_CHANNEL_H
#include <stdint.h>
// One private socket, one request, one bounded reply. No user path crosses this API.
int archive_launch(const char *executable, int *channel, int *child);
int archive_send(int fd, const void *bytes, uint32_t length);
int archive_receive(int fd, void **bytes, uint32_t *length);
int archive_finish(int fd, int child, int cancel);
#endif
