<!-- Old link: money is a tab on the main page now. -->
<script>
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  onMount(() => goto('/#money', { replaceState: true }));
</script>
