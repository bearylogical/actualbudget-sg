<!-- Old link: history is a tab on the main page now. -->
<script>
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  onMount(() => goto('/#history', { replaceState: true }));
</script>
